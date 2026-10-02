"""The pending_source_pull leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the leg stages on the born seats 'driven'/'foreign'/
# 'revised' and the scratch born field — it needs the ownerless-
# backoff leg's seats released and must be done before the
# fencing-loss leg's pair and the revision legs take the born seats
# over.
RUNS_AFTER = frozenset({'scenario_ownerless_remote_backoff'})
RUNS_BEFORE = frozenset({'scenario_sim_cyclic_fencing_loss_demote',
                         'scenario_incompatible_revision',
                         'scenario_model_revision'})


# --------------------------------------------------------------------
# The pending-source checkpoint-pull contract — the per-revision lane
# evidence for #1315's fix (WW-LCM-001 continuity — the pull path is
# the standby's convergence mechanism): a standby's checkpoint pulls
# against a live, serving source must fetch and apply checkpoints,
# and a transient early failure must not latch past the condition
# that caused it. The defect the contract answers latched a permanent
# degraded{fetch …: Resource temporarily unavailable} for the life of
# the standby process — the pull worker's stalled fetch stranding the
# pull path — while byte-exact replications of the same request to
# the same endpoint answered instantly and a post-restart track of
# the same source converged in seconds. The staged reproduction, on
# the leg's own scratch sim-serve field so the deployed pair's plant
# and claim arbitration are never touched:
#
# - the born-active source launches against the frozen born field —
#   docker pause leaves the listener's backlog accepting the attach
#   while no claim verdict answers, so the source stands pending:
#   role standby, an honest sync, no observed claim, a live process.
#   Its paced scans block in field I/O under the shared monitor
#   lock, so a tracker's checkpoint fetch against it meets the
#   bounded misses — refused, stalled, in-flight — the defect
#   latched.
#
# - the tracking standby launches inside the pending window with
#   --standby naming the pending seat — the label the defect
#   reproduced on. It may serve unsynchronized, degraded, or
#   orphaned through the window; only tracking is a verdict it
#   cannot hold behind a source that owns nothing.
#
# - the field thaws: the pending source's first answered contact
#   lands its deferred conditional grant, it claims the field, and
#   its checkpoints begin stamping ownership — the standby must
#   converge to a non-degraded verdict inside the documented bound
#   the fix names, without a process restart: tracking once the
#   field is owned, orphaned while the served checkpoint still
#   reports no owner. The converged verdict must hold across
#   sustained reads — subsequent pulls keep landing — and the same
#   endpoint must answer a direct /checkpoint fetch; the tracker's
#   durable journal carries the convergence evidence (a tracking
#   verdict's applied checkpoints deliver the planted journaled
#   point's point_changed; an orphaned verdict owes its
#   field_orphaned) and exactly one run boundary — recovery inside
#   one process lifetime, never the restart the defect demanded.
#
# - the positive control — a same-shape --standby tracking the
#   deployed incumbent on the pair's own plant — converges
#   identically; the deployed pair is undisturbed throughout: same
#   field owner, claim held, ticks advancing; every staged seat and
#   the scratch field are removed again.
#
# Named diagnostics: pending-source-pull-failed tags the contract
# clauses — a standby still degraded past the bound, a tracker that
# exits or converges only by restarting, a verdict the field's claim
# state cannot support, pulls that keep missing the served endpoint,
# convergence the durable or served journal cannot prove, a control
# standby that fails, a disturbed deployed pair — and
# pending-source-pull-nondeterministic tags the instability the
# contract does not answer for: a refused staging call, a starved
# watch, a source that never serves after the thaw, an unrestored
# rig, two passes whose digests diverge. A staged run that predates
# the contract — the pending source exits on the frozen field, or
# the standby replays the pending window's verbatim refusal forever
# while the same endpoint serves — reports inconclusive: every build
# predates it until #1315's fix lands. The unchecked-diagnostic
# self-check replays the judge over planted negatives and reports
# pending-source-pull-unchecked for any that slip through.

PULL_PENDING = 30   # bound on the frozen-field window's staged watch
PULL_SETTLE = 45    # bound on each post-thaw convergence wait — the
                    # documented recovery bound's rig-scale cover
PULL_POLL = 0.4     # cadence polling a seat's monitor mid-stage
WINDOW_READS = 3    # served views the pending window must span
WINDOW_HOLD = 8     # seconds the frozen window holds once views land —
                    # long enough for the pull cycle to fetch and miss
SUSTAIN_READS = 3   # post-convergence reads the verdict must hold
ADOPT_POINT = 1011  # pump_station's journaled writable in-point — the
                    # planted write the tracker's apply must carry
SOURCE_SEAT = 'driven'
TRACK_SEAT = 'foreign'
CONTROL_SEAT = 'revised'
BORN_SEATS_USED = ('driven', 'foreign', 'revised')
DIAG_FAILED = 'pending-source-pull-failed'
DIAG_NONDET = 'pending-source-pull-nondeterministic'
DIAG_UNCHECKED = 'pending-source-pull-unchecked'

# The sync verdicts an honest standby may serve behind a source that
# has not claimed yet — 'tracking' requires an owning source, so its
# appearance inside the pending window is the contract's lie.
WINDOW_SYNC = ('unsynchronized', 'degraded', 'orphaned')


def _sync_kind(report):
    """The served StandbySync's variant name — 'unsynchronized' is a
    bare string, the rest are single-key objects."""
    sync = (report or {}).get('sync')
    if isinstance(sync, str):
        return sync
    if isinstance(sync, dict) and sync:
        return next(iter(sync))
    return None


def _sync_detail(report):
    """The degraded verdict's served detail — the fetch stage the
    pull reached, which the defect replayed verbatim for process
    life."""
    sync = (report or {}).get('sync')
    if isinstance(sync, dict):
        return (sync.get('degraded') or {}).get('detail')
    return None


def _seat_role(ctx, seat):
    return _try_role(ctx, ctx[seat])


def _seat_view(ctx, seat):
    """One read of a born seat's served surface: role, sync verdict
    and degraded detail, observed field claim, tick — the window's
    evidence tuple."""
    report = _try_role(ctx, ctx[seat])
    if report is None:
        return None
    return {'role': report.get('role'),
            'sync': _sync_kind(report),
            'detail': _sync_detail(report),
            'field_claim': report.get('field_claim'),
            'tick': report.get('tick')}


def _member_view(ctx, name):
    """One deployed member's served surface — the undisturbed-pair
    evidence: role, observed claim, advancing tick, the member's
    tracking verdict."""
    report = _try_role(ctx, ctx[name])
    if report is None:
        return None
    return {'role': report.get('role'),
            'sync': _sync_kind(report),
            'field_claim': report.get('field_claim'),
            'tick': report.get('tick')}


def _seat_state(ctx, seat):
    """The seat container's process verdict through the runner's
    read-only state lever."""
    state = ctx.get('born_controller_state')
    if state is None:
        return None
    try:
        return state(seat)
    except Exception:
        return None


def _checkpoint_probe(ctx, seat, timeout):
    """One direct checkpoint fetch against the seat's published
    monitor — the 'same endpoint answers' evidence the issue names:
    the byte-exact request that returned instantly even on the
    defect build."""
    try:
        status, body = http_json('GET', ctx[seat] + '/checkpoint',
                                 timeout=timeout)
        return {'status': status, 'tick': (body or {}).get('tick')}
    except Exception as exc:
        return {'error': str(exc)[:160]}


def _seat_journal(ctx, seat):
    """The seat's durable --journal-file records — the runner-owned
    file the born launch resets at launch."""
    path = (ctx.get('journal_files') or {}).get(seat)
    if not path or not Path(path).is_file():
        return []
    return _journal_entries(path)


def _adoptions(items, body_key='entry'):
    """The planted point's journaled point_changed records — the
    adopted value a tracking verdict's applied checkpoints must
    carry into the standby's own durable evidence."""
    out = []
    for item in items:
        event = ((item.get(body_key) if body_key in item else item)
                 or {}).get('event') or {}
        changed = event.get('point_changed')
        if isinstance(changed, dict) \
                and changed.get('point') == ADOPT_POINT \
                and (changed.get('to') or {}).get('bool') is True:
            out.append(changed)
    return out


def _orphans(items, body_key='entry'):
    """The journaled field_orphaned episodes — the orphaned verdict's
    durable record."""
    out = []
    for item in items:
        event = ((item.get(body_key) if body_key in item else item)
                 or {}).get('event') or {}
        orphan = event.get('field_orphaned')
        if isinstance(orphan, dict):
            out.append(orphan)
    return out


def _journal_audit(ctx, seat):
    """The seat's convergence evidence on both journal surfaces: the
    durable --journal-file's run boundaries (one process lifetime),
    orphaned episodes, and planted adoptions, plus the served
    /journal mirror."""
    items = _seat_journal(ctx, seat)
    boundaries = [item['run_boundary'].get('run') for item in items
                  if isinstance(item.get('run_boundary'), dict)]
    served = None
    try:
        _, payload = http_json('GET', ctx[seat] + '/journal?since=0')
        served_items = _journal_list(payload)
        served = {'adoptions': _adoptions(served_items),
                  'orphans': _orphans(served_items)}
    except Exception:
        pass
    return {'boundaries': boundaries,
            'entries': len(items),
            'adoptions': _adoptions(items),
            'orphans': _orphans(items),
            'served': served}


def _plant_write(ctx, seat):
    """The journaled in-point write the tracking verdict's applied
    checkpoints must carry to the standby's durable journal — POST
    /command on the claimed source. Returns the served receipt's
    outcome kind, or None when the write never receipts."""
    try:
        _, receipt = http_json('POST', ctx[seat] + '/command', {
            'command': {'write_value': {'point': ADOPT_POINT,
                                        'kind': 'bool',
                                        'value': {'bool': True}}},
            'actor': 'qa-pending-pull'})
    except Exception:
        return None
    outcome = (receipt or {}).get('outcome') or {}
    for kind in ('applied', 'accepted', 'rejected'):
        if kind in outcome:
            return kind
    return 'other'


def _watch_source_pending(ctx):
    """The born-active's pending surface before the tracker stages:
    the first served standby/no-claim report — or the process
    verdict when the launch died on the frozen field instead."""
    views, state = [], None
    deadline = time.monotonic() + PULL_PENDING
    while time.monotonic() < deadline:
        view = _seat_view(ctx, SOURCE_SEAT)
        if view is not None:
            views.append(view)
            break
        state = _seat_state(ctx, SOURCE_SEAT)
        if (state or {}).get('running') is False \
                and not (state or {}).get('absent'):
            break
        time.sleep(PULL_POLL)
    return {'views': views, 'state': state}


def _watch_pending_window(ctx):
    """The frozen-field window's staged evidence: the tracking
    standby's and the pending source's served reads while the standby
    pulls against the pending endpoint — WINDOW_READS views each
    spanning at least WINDOW_HOLD seconds, so the pull cycle has
    fetched and missed at least once. Ends early on either seat's
    process ending — a dead source is the dependency contract's
    shape, not this leg's."""
    tracker, source, details, states = [], [], [], {}
    start = time.monotonic()
    deadline = start + PULL_PENDING
    while time.monotonic() < deadline:
        view = _seat_view(ctx, TRACK_SEAT)
        if view is not None:
            tracker.append(view)
            detail = view.get('detail')
            if detail and detail not in details:
                details.append(detail)
        view = _seat_view(ctx, SOURCE_SEAT)
        if view is not None:
            source.append(view)
        early = False
        for seat in (TRACK_SEAT, SOURCE_SEAT):
            state = _seat_state(ctx, seat)
            if state is not None:
                states[seat] = state
            # absent is not a process verdict — the container may be
            # mid-create; only an inspected not-running state ends the
            # window early.
            if (states.get(seat) or {}).get('running') is False \
                    and not (states.get(seat) or {}).get('absent'):
                early = True
        if early or (len(tracker) >= WINDOW_READS
                     and len(source) >= WINDOW_READS
                     and time.monotonic() - start >= WINDOW_HOLD):
            break
        time.sleep(PULL_POLL)
    return {'tracker': tracker, 'source': source,
            'details': details, 'states': states}


def _await_report(ctx, seat, match, bound):
    """Poll the seat's /role until `match(report)` holds; returns the
    last report served either way — the unconverged surface is the
    evidence of what the seat reported instead."""
    latest = []
    wait_for(lambda: (lambda report: latest.append(report) or
                      (report if report is not None
                       and match(report) else None))
             (_seat_role(ctx, seat)),
             time.monotonic() + bound, interval=PULL_POLL)
    return latest[-1] if latest else None


def _await_verdict(ctx, seat, expected, bound):
    """Poll the standby's /role until the served sync verdict is
    `expected` (or, when None, any non-degraded tracking/orphaned);
    returns (last report, post-thaw degraded details) — the verbatim
    details the latch classifier reads."""
    latest, details = [], []

    def check():
        report = _seat_role(ctx, seat)
        if report is None:
            return None
        latest.append(report)
        detail = _sync_detail(report)
        if detail and detail not in details:
            details.append(detail)
        kind = _sync_kind(report)
        if report.get('role') != 'standby':
            return None
        if expected is None:
            return report if kind in ('tracking', 'orphaned') \
                else None
        return report if kind == expected else None

    wait_for(check, time.monotonic() + bound, interval=PULL_POLL)
    return (latest[-1] if latest else None), details


def _tracking(report):
    return (report or {}).get('role') == 'standby' \
        and _sync_kind(report) == 'tracking'


def _pull_pass(ctx, number):
    """One pass over the pending-source pull contract: freeze the
    scratch field, launch the born-active source into pending, stage
    the tracking standby and the deployed-incumbent control inside
    the window, thaw, and collect the convergence, the pull and
    journal evidence, and the deployed pair's undisturbedness.
    Returns (record, evidence); the judge replays the record."""
    record = {'pass': number}
    evidence = {'pass': number}
    incumbent = _pair_active(ctx)
    if incumbent is None:
        evidence['inconclusive'] = (
            'the deployed pair reports no field-owning member — the '
            'positive control\'s tracking source never settled')
        return record, evidence
    member = 'standby' if incumbent == 'active' else 'active'
    record['incumbent'] = incumbent
    record['member'] = member
    record['incumbent_before'] = _member_view(ctx, incumbent)
    record['member_before'] = _member_view(ctx, member)
    if (record['incumbent_before'] or {}).get('role') != 'active' \
            or (record['incumbent_before'] or {}).get('field_claim') \
            != 'held' \
            or (record['member_before'] or {}).get('role') != 'standby' \
            or (record['member_before'] or {}).get('sync') \
            != 'tracking':
        evidence['inconclusive'] = (
            'the deployed pair is not in the settled shape the '
            'staging needs — the incumbent must hold the field with '
            'its member tracking: '
            + json.dumps({'incumbent': record['incumbent_before'],
                          'member': record['member_before']})[:200])
        return record, evidence
    paused = False
    try:
        try:
            field = ctx['start_born_field']('serving')
            record['remote'] = field['remote']
            ctx['pause_born_field']()
            paused = True
            ctx['start_born_controller'](SOURCE_SEAT,
                                       record['remote'])
        except Exception as exc:
            record['stage_error'] = str(exc)[:300]
            return record, evidence
        # The pending surface must be served — and held — before the
        # standby stages: the window is only real while the tracked
        # seat cannot answer a claim verdict.
        record['pending'] = _watch_source_pending(ctx)
        if (record['pending'].get('state') or {}).get('running') \
                is False \
                and not (record['pending'].get('state') or {}) \
                .get('absent'):
            evidence['inconclusive'] = (
                'the born-active exited on the frozen field rather '
                'than standing pending — the staged run predates the '
                'pending-source contract the pull recovery rides on')
            return record, evidence
        try:
            record['tracker_launch'] = ctx['start_born_controller'](
                TRACK_SEAT, record['remote'], standby=SOURCE_SEAT)
            record['control_launch'] = ctx['start_born_controller'](
                CONTROL_SEAT, ctx['plant_remote'], standby=incumbent)
        except Exception as exc:
            record['stage_error'] = str(exc)[:300]
            return record, evidence
        record['window'] = _watch_pending_window(ctx)
        states = (record['window'].get('states') or {})
        if (states.get(SOURCE_SEAT) or {}).get('running') is False \
                and not (states.get(SOURCE_SEAT) or {}).get('absent'):
            evidence['inconclusive'] = (
                'the pending source exited inside the frozen '
                'window — no serving source can follow, so the '
                'staged run predates the contract')
            return record, evidence
        record['window_probe'] = _checkpoint_probe(
            ctx, SOURCE_SEAT, 3)
        try:
            ctx['unpause_born_field']()
            paused = False
        except Exception as exc:
            record['stage_error'] = str(exc)[:300]
            return record, evidence
        # The thaw: the deferred conditional grant lands on the first
        # answered contact and the source claims and serves.
        record['source_after'] = _await_report(
            ctx, SOURCE_SEAT,
            lambda report: report.get('role') == 'active'
            and report.get('field_claim') == 'held',
            PULL_SETTLE)
        record['source_claimed'] = bool(
            record['source_after']
            and record['source_after'].get('role') == 'active'
            and record['source_after'].get('field_claim') == 'held')
        record['serve_probe'] = _checkpoint_probe(
            ctx, SOURCE_SEAT, 10)
        record['source_state'] = _seat_state(ctx, SOURCE_SEAT)
        if (record['source_state'] or {}).get('running') is False \
                and not (record['source_state'] or {}).get('absent'):
            evidence['inconclusive'] = (
                'the pending source exited at the thaw rather than '
                'claiming — the deferred-grant dependency never '
                'produced a serving source')
            return record, evidence
        if record['source_claimed']:
            record['expected'] = 'tracking'
            record['planted'] = _plant_write(ctx, SOURCE_SEAT)
        elif (record['serve_probe'] or {}).get('status') == 200:
            # The field's claim state still reports no owner — the
            # honest convergence is the orphaned verdict.
            record['expected'] = 'orphaned'
        else:
            record['expected'] = None
        # The contract's core: the standby that entered tracking
        # inside the pending window must converge once the source
        # serves — no restart, no latched refusal.
        record['tracker_after'], record['post_details'] = \
            _await_verdict(ctx, TRACK_SEAT, record.get('expected'),
                           PULL_SETTLE)
        kind = _sync_kind(record.get('tracker_after'))
        record['converged'] = kind if kind in ('tracking', 'orphaned') \
            and (record.get('tracker_after') or {}).get('role') \
            == 'standby' else None
        record['pull_probe'] = _checkpoint_probe(ctx, SOURCE_SEAT, 10)
        sustained = []
        for _ in range(SUSTAIN_READS):
            report = _seat_role(ctx, TRACK_SEAT)
            sustained.append(_sync_kind(report)
                             if report is not None else 'unserved')
            time.sleep(PULL_POLL)
        record['sustained'] = sustained
        # The durable half: the planted adoption (tracking) or the
        # orphaned episode must land on the tracker's journal — give
        # the record a bound before auditing.
        waited = {'tracking': 'adoptions',
                  'orphaned': 'orphans'}.get(record.get('converged'))
        if waited:
            wait_for(
                lambda: (_journal_audit(ctx, TRACK_SEAT).get(waited)
                         or None),
                time.monotonic() + PULL_SETTLE, interval=PULL_POLL)
        record['journals'] = {
            seat: _journal_audit(ctx, seat)
            for seat in BORN_SEATS_USED}
        record['control_after'] = _await_report(
            ctx, CONTROL_SEAT, _tracking, PULL_SETTLE)
        record['states'] = {seat: _seat_state(ctx, seat)
                            for seat in BORN_SEATS_USED}
        record['incumbent_after'] = _member_view(ctx, incumbent)
        record['member_after'] = _member_view(ctx, member)
        # The pre-#1315 shape: the pull path stranded on the pending
        # window's verbatim refusal — every post-thaw verdict replays
        # the same detail while the endpoint itself already serves.
        # The fix's contract cannot be asserted on the build that
        # carries the defect.
        if record.get('converged') is None:
            detail = _sync_detail(record.get('tracker_after'))
            windowed = (record.get('window') or {}).get('details') or []
            post = record.get('post_details') or []
            running = ((record.get('states') or {}).get(TRACK_SEAT)
                       or {}).get('running') is True
            if detail is not None and detail in windowed \
                    and post and all(item in windowed for item in post) \
                    and (record.get('pull_probe') or {}).get('status') \
                    == 200 and running:
                evidence['inconclusive'] = (
                    'the standby never recovered — every post-thaw '
                    'pull verdict replays the pending window\'s '
                    'verbatim refusal while the same endpoint serves '
                    'its checkpoint — the staged run predates the '
                    'transient-failure recovery contract')
                return record, evidence
    finally:
        if paused:
            try:
                ctx['unpause_born_field']()
            except Exception:
                pass
        errors = []
        for seat in BORN_SEATS_USED:
            try:
                ctx['stop_born_controller'](seat)
            except Exception as exc:
                errors.append(seat + ': ' + str(exc)[:120])
        try:
            ctx['stop_born_field']()
        except Exception as exc:
            errors.append('born-field: ' + str(exc)[:120])
        record['teardown'] = {
            'errors': errors,
            'absent': all((_seat_state(ctx, seat) or {}).get('absent')
                          for seat in BORN_SEATS_USED)}
    return record, evidence


def _incumbent_undisturbed(before, after):
    """The deployed incumbent was never disturbed: still active, still
    holding the field's claim, its tick advancing across the staged
    window."""
    return (after or {}).get('role') == 'active' \
        and (after or {}).get('field_claim') == 'held' \
        and isinstance((before or {}).get('tick'), int) \
        and isinstance((after or {}).get('tick'), int) \
        and after['tick'] > before['tick']


def _member_undisturbed(before, after):
    """The deployed member was never disturbed: still the tracking
    standby, claim observed held, its tick advancing."""
    return (after or {}).get('role') == 'standby' \
        and (after or {}).get('sync') == 'tracking' \
        and isinstance((before or {}).get('tick'), int) \
        and isinstance((after or {}).get('tick'), int) \
        and after['tick'] > before['tick']


def _judge_pull(record, note):
    """Audit one pass's record — replayable, so the self-check can
    hand it planted negatives. `note(key, diagnostic, detail)`
    records each clause the record violates: DIAG_FAILED tags the
    contract clauses — a pending-window tracker reporting a verdict
    it cannot hold, a standby that never converges or converges on
    the wrong verdict, a convergence by exit or restart, pulls that
    keep missing a serving endpoint, unjournaled convergence
    evidence, a failed control, a disturbed deployed pair — and
    DIAG_NONDET tags the instability the contract does not answer
    for: a refused staging call, a starved watch, a source that never
    serves, a sweep that leaves staged seats behind."""
    def failed(key, detail):
        note(key, DIAG_FAILED, detail)

    def nondet(key, detail):
        note(key, DIAG_NONDET, detail)

    if record.get('stage_error') is not None:
        nondet('stage', 'the pending-source staging never completed: '
               + str(record['stage_error']))
        return

    pending = record.get('pending') or {}
    views = pending.get('views') or []
    if not views:
        nondet('source-watch', 'the born-active never served the '
               'pending surface while the field stood frozen — the '
               'window never staged')
    elif not all(view.get('role') == 'standby'
                 and view.get('field_claim') is None
                 for view in views):
        failed('source-pending', 'the born-active reported a role it '
               'cannot hold while the field could not answer — '
               'standby with no observed claim is the deferred '
               'grant\'s only honest report: '
               + json.dumps(views)[:300])
    state = pending.get('state')
    if state is not None and state.get('running') is not True:
        nondet('source-exit', 'the pending source was not running '
               'inside the window — the staging never produced the '
               'serving source')

    window = record.get('window') or {}
    tracker_views = window.get('tracker') or []
    if not tracker_views:
        nondet('tracker-watch', 'the tracking standby never served a '
               'report inside the pending window — the pending-window '
               'pull was never staged')
    elif not all(view.get('role') == 'standby'
                 and view.get('sync') in WINDOW_SYNC
                 for view in tracker_views):
        failed('window-honest', 'the tracking standby reported a '
               'verdict it cannot hold inside the pending window — '
               'tracking requires an owning source: '
               + json.dumps(tracker_views)[:300])
    source_views = window.get('source') or []
    if not source_views:
        nondet('source-window', 'the pending source never served its '
               'surface inside the window — the window the standby '
               'entered was never staged')
    elif not all(view.get('role') == 'standby'
                 and view.get('field_claim') is None
                 for view in source_views):
        failed('window-source', 'the tracked source left pending '
               'mid-window — the window the standby entered was '
               'never staged: ' + json.dumps(source_views)[:300])
    if (record.get('window_probe') or {}).get('status') == 200:
        nondet('window-probe', 'the pending source answered its '
               'checkpoint inside the frozen window — the bounded '
               'misses the standby\'s pull entered were never '
               'staged')
    target = (record.get('tracker_launch') or {}).get('standby')
    details = window.get('details') or []
    if target and details \
            and not any(target in detail for detail in details):
        failed('pull-target', 'the pending-window pull misses name '
               'an endpoint other than the staged source: '
               + json.dumps(details)[:200])

    journals = record.get('journals') or {}
    states = record.get('states') or {}
    expected = record.get('expected')
    if expected is None:
        nondet('source-serving', 'the thawed field never produced a '
               'serving source — the checkpoint probe still refuses: '
               + json.dumps(record.get('serve_probe'))[:200])
    else:
        tracker_state = states.get(TRACK_SEAT) or {}
        converged = record.get('converged')
        if tracker_state.get('running') is False \
                and not tracker_state.get('absent'):
            failed('tracker-exit', 'the tracking standby\'s process '
                   'ended — the pending-window pull\'s recovery '
                   'belongs to a live run, not a restart: '
                   + json.dumps(tracker_state)[:200])
        elif tracker_state.get('running') is not True:
            nondet('tracker-state', 'the tracking standby\'s '
                   'process verdict never landed: '
                   + json.dumps(tracker_state)[:200])
        elif converged is None:
            failed('converge-bound', 'the standby stayed '
                   + str(_sync_kind(record.get('tracker_after')))
                   + ' past the documented bound instead of '
                   'converging ' + expected + ' — the transient '
                   'pending-window failure latched: '
                   + json.dumps(record.get('post_details')
                                or [])[:240])
        elif _sync_kind(record.get('tracker_after')) != converged:
            failed('converge-honest', 'the pass recorded '
                   + converged + ' while the standby\'s last served '
                   'sync stayed '
                   + str(_sync_kind(record.get('tracker_after')))
                   + ' — convergence was asserted over a degraded '
                   'surface')
        elif converged != expected:
            failed('converge-verdict', 'the standby converged '
                   + converged + ' but the field\'s claim state '
                   'says ' + expected + ' — the verdict is not the '
                   'honest one')
        else:
            tj = journals.get(TRACK_SEAT) or {}
            if not tj.get('boundaries'):
                nondet('journal-file', 'the tracking standby\'s '
                       'durable journal carries no run boundary — '
                       'the file never staged')
            elif tj['boundaries'] != [1]:
                failed('restart', 'the tracking standby\'s durable '
                       'journal carries more than one process '
                       'lifetime — it converged by restarting, not '
                       'through the pull path: '
                       + json.dumps(tj['boundaries']))
            probe = record.get('pull_probe') or {}
            if probe.get('status') != 200:
                failed('pull-endpoint', 'the staged endpoint still '
                       'refuses its checkpoint after the converged '
                       'verdict: ' + json.dumps(probe)[:160])
            sustained = record.get('sustained') or []
            if len(sustained) < SUSTAIN_READS \
                    or not all(kind == expected for kind in sustained):
                failed('pull-sustained', 'the converged verdict did '
                       'not hold — subsequent pulls kept missing: '
                       + json.dumps(sustained))
            if tj.get('served') is None:
                nondet('journal-read', 'the tracking standby\'s '
                       'served journal dropped the audit read')
            if converged == 'tracking':
                if not (journals.get(SOURCE_SEAT) or {}) \
                        .get('adoptions'):
                    nondet('planted-write', 'the planted journaled '
                           'write never journaled on the source — '
                           'the adoption stimulus never landed')
                elif not tj.get('adoptions'):
                    failed('journal-convergence', 'the tracking '
                           'verdict\'s adopted checkpoints never '
                           'journaled the planted point\'s '
                           'point_changed — the convergence evidence '
                           'does not stand')
                elif tj.get('served') is not None \
                        and not tj['served'].get('adoptions'):
                    failed('journal-served', 'the standby\'s served '
                           'journal never reported the planted '
                           'adoption the durable file carries')
            else:
                if not tj.get('orphans'):
                    failed('journal-convergence', 'the orphaned '
                           'verdict\'s field_orphaned record never '
                           'journaled — the convergence evidence '
                           'does not stand')
                elif tj.get('served') is not None \
                        and not tj['served'].get('orphans'):
                    failed('journal-served', 'the standby\'s served '
                           'journal never reported the orphaned '
                           'episode the durable file carries')

    control = record.get('control_after')
    control_state = states.get(CONTROL_SEAT) or {}
    cj = journals.get(CONTROL_SEAT) or {}
    if control_state.get('running') is False \
            and not control_state.get('absent'):
        failed('control-exit', 'the positive-control standby\'s '
               'process ended: ' + json.dumps(control_state)[:200])
    elif control_state.get('running') is not True:
        nondet('control-state', 'the positive-control standby\'s '
               'process verdict never landed: '
               + json.dumps(control_state)[:200])
    elif not _tracking(control):
        failed('control', 'the same-shape standby tracking the '
               'serving deployed incumbent never converged '
               'tracking: ' + json.dumps(control)[:200])
    if cj.get('boundaries') and cj.get('boundaries') != [1]:
        failed('control-restart', 'the positive-control standby '
               'restarted — ' + json.dumps(cj.get('boundaries')))

    if not _incumbent_undisturbed(record.get('incumbent_before'),
                                  record.get('incumbent_after')):
        failed('incumbent', 'the deployed incumbent was disturbed — '
               'role, claim, or liveness changed under the staging: '
               + json.dumps(record.get('incumbent_after'))[:200])
    if not _member_undisturbed(record.get('member_before'),
                               record.get('member_after')):
        failed('member', 'the deployed standby member was disturbed '
               '— it stopped tracking or stopped advancing: '
               + json.dumps(record.get('member_after'))[:200])
    teardown = record.get('teardown') or {}
    if teardown.get('errors'):
        nondet('teardown', 'the rig sweep refused: '
               + '; '.join(teardown['errors'])[:240])
    elif teardown.get('absent') is not True:
        nondet('restored', 'a staged seat survived the sweep — the '
               'launch configuration was not restored')


def _digest_pull(record, violations):
    """The pass's normalized verdict record — identical across clean
    passes; each field carries the recorded disposition only while no
    violation names it."""
    def clean(*keys):
        return not any(key in violations for key in keys)
    return {
        'window': 'pending-entered'
            if clean('stage', 'source-watch', 'source-pending',
                     'source-exit', 'tracker-watch', 'window-honest',
                     'window-source', 'window-probe', 'pull-target')
            else 'defect',
        'source': ('claimed' if record.get('source_claimed')
                   else 'serving-unclaimed')
            if clean('source-serving') else 'defect',
        'tracker': (record.get('converged') or 'unconverged')
            if clean('tracker-exit', 'converge-bound',
                     'converge-honest', 'converge-verdict')
            else 'defect',
        'pulls': 'landed'
            if clean('pull-endpoint', 'pull-sustained')
            else 'defect',
        'journal': 'standing'
            if clean('restart', 'journal-convergence',
                     'journal-served', 'journal-read', 'planted-write')
            else 'defect',
        'control': 'tracking'
            if clean('control', 'control-exit', 'control-restart')
            else 'defect',
        'pair': 'undisturbed'
            if clean('incumbent', 'member') else 'defect',
        'restored': 'clean'
            if clean('teardown', 'restored') else 'defect'}


PENDING_DETAIL = ('fetch from dcs-hw-qa-1-d:8082: Resource '
                  'temporarily unavailable')
ADOPTION = {'point': ADOPT_POINT, 'from': {'bool': False},
            'to': {'bool': True}}


def _pull_self_check():
    """The leg's unchecked-diagnostic self-test: replay the judge
    over each planted negative — every recorded failure shape must
    name DIAG_FAILED; the instability shapes must report
    DIAG_NONDET. Returns the negative names the judge let through."""
    slipped = []

    def clean_record():
        return {
            'remote': 'dcs-hw-qa-1-born-plant:9003',
            'incumbent': 'active', 'member': 'standby',
            'incumbent_before': {'role': 'active', 'tick': 10,
                                 'field_claim': 'held',
                                 'sync': 'unsynchronized'},
            'member_before': {'role': 'standby', 'tick': 10,
                              'field_claim': 'held',
                              'sync': 'tracking'},
            'pending': {
                'views': [{'role': 'standby', 'sync': 'unsynchronized',
                           'detail': None, 'field_claim': None,
                           'tick': 1}],
                'state': {'running': True, 'exit': None,
                          'absent': False}},
            'tracker_launch': {'standby': 'dcs-hw-qa-1-d:8082'},
            'control_launch': {'standby': 'dcs-hw-qa-1-a:8080'},
            'window': {
                'tracker': [{'role': 'standby', 'sync': 'degraded',
                             'detail': PENDING_DETAIL,
                             'field_claim': None, 'tick': 2}],
                'source': [{'role': 'standby', 'sync': 'unsynchronized',
                            'detail': None, 'field_claim': None,
                            'tick': 2}],
                'details': [PENDING_DETAIL], 'states': {}},
            'window_probe': {'error': 'timed out'},
            'source_after': {'role': 'active', 'field_claim': 'held'},
            'source_claimed': True,
            'serve_probe': {'status': 200, 'tick': 9},
            'expected': 'tracking',
            'planted': 'accepted',
            'tracker_after': {'role': 'standby',
                              'sync': {'tracking': {'aligned': 9}},
                              'field_claim': 'held'},
            'post_details': [],
            'converged': 'tracking',
            'pull_probe': {'status': 200, 'tick': 10},
            'sustained': ['tracking'] * SUSTAIN_READS,
            'journals': {
                'driven': {'boundaries': [1], 'entries': 3,
                           'adoptions': [ADOPTION], 'orphans': [],
                           'served': {'adoptions': [ADOPTION],
                                      'orphans': []}},
                'foreign': {'boundaries': [1], 'entries': 4,
                            'adoptions': [ADOPTION], 'orphans': [],
                            'served': {'adoptions': [ADOPTION],
                                       'orphans': []}},
                'revised': {'boundaries': [1], 'entries': 2,
                            'adoptions': [], 'orphans': [],
                            'served': {'adoptions': [],
                                       'orphans': []}}},
            'control_after': {'role': 'standby',
                              'sync': {'tracking': {'aligned': 30}}},
            'states': {
                'driven': {'running': True, 'exit': None,
                           'absent': False},
                'foreign': {'running': True, 'exit': None,
                            'absent': False},
                'revised': {'running': True, 'exit': None,
                            'absent': False}},
            'incumbent_after': {'role': 'active', 'tick': 30,
                                'field_claim': 'held',
                                'sync': 'unsynchronized'},
            'member_after': {'role': 'standby', 'tick': 30,
                             'field_claim': 'held',
                             'sync': 'tracking'},
            'teardown': {'errors': [], 'absent': True}}

    def expect(name, mutate, diagnostic=DIAG_FAILED):
        record = clean_record()
        mutate(record)
        found = {}
        _judge_pull(record,
                    lambda key, diag, detail:
                    found.setdefault(key, diag))
        if diagnostic not in found.values():
            slipped.append(name)

    def tracker(mutate):
        return lambda record: mutate(record['tracker_after'])

    # The doctored negatives the issue names — convergence asserted
    # while the standby's sync stays degraded past the documented
    # bound, and each wrong disposition around it: a never-converged
    # tracker, a wrong or dishonest verdict, a convergence by exit or
    # restart, pulls that keep missing the served endpoint,
    # unjournaled evidence, a failed control, a disturbed pair, a
    # dishonest window.
    expect('converged-but-degraded', lambda record:
           record['tracker_after'].update(
               {'sync': {'degraded': {'detail': 'fetch from '
                                      'dcs-hw-qa-1-d:8082: checkpoint '
                                      'pull still in flight'}}}))
    expect('never-converged', lambda record:
           record.update(
               {'converged': None,
                'tracker_after': {'role': 'standby',
                                  'sync': {'degraded': {'detail':
                                           'checkpoint pull still '
                                           'in flight'}}},
                'post_details': ['checkpoint pull still in flight']}))
    expect('wrong-verdict', lambda record:
           record.update({'converged': 'orphaned'}))
    expect('tracker-exited', lambda record:
           record['states']['foreign'].update(
               {'running': False, 'exit': 1}))
    expect('tracker-restarted', lambda record:
           record['journals']['foreign'].update(
               {'boundaries': [1, 2]}))
    expect('pull-refuses', lambda record:
           record.update({'pull_probe': {'error': 'timed out'}}))
    expect('pull-lapses', lambda record:
           record['sustained'].append('degraded'))
    expect('adoption-unjournaled', lambda record:
           record['journals']['foreign'].update({'adoptions': []}))
    expect('served-journal-silent', lambda record:
           record['journals']['foreign']
           .update({'served': {'adoptions': [], 'orphans': []}}))
    expect('control-unconverged', lambda record:
           record.update(
               {'control_after': {'role': 'standby',
                                  'sync': 'unsynchronized'}}))
    expect('control-exited', lambda record:
           record['states']['revised'].update(
               {'running': False, 'exit': 1}))
    expect('control-restarted', lambda record:
           record['journals']['revised'].update(
               {'boundaries': [1, 2]}))
    expect('incumbent-disturbed', lambda record:
           record['incumbent_after'].update(
               {'role': 'standby', 'field_claim': None}))
    expect('member-disturbed', lambda record:
           record['member_after'].update({'sync': 'degraded'}))
    expect('window-tracker-tracking', lambda record:
           record['window']['tracker'][0].update(
               {'sync': 'tracking', 'detail': None}))
    expect('window-source-active', lambda record:
           record['window']['source'][0].update(
               {'role': 'active', 'field_claim': 'held'}))
    expect('pending-reports-active', lambda record:
           record['pending']['views'][0].update(
               {'role': 'active', 'field_claim': 'held'}))
    expect('pulls-aimed-elsewhere', lambda record:
           record['window'].update(
               {'details': ['fetch from dcs-hw-qa-1-z:8082: '
                            'refused']}))
    # The instability shapes must report nondeterministic: a refused
    # staging call, a starved watch, a source that never serves, a
    # dropped journal read, a refused sweep.
    expect('stage-refused', lambda record:
           record.update({'stage_error': 'docker run failed'}),
           DIAG_NONDET)
    expect('source-watch-starved', lambda record:
           record['pending'].update({'views': []}), DIAG_NONDET)
    expect('tracker-watch-starved', lambda record:
           record['window'].update({'tracker': []}), DIAG_NONDET)
    expect('window-probe-served', lambda record:
           record.update({'window_probe': {'status': 200,
                                           'tick': 4}}),
           DIAG_NONDET)
    expect('source-never-serves', lambda record:
           record.update({'expected': None}), DIAG_NONDET)
    expect('journal-read-dropped', lambda record:
           record['journals']['foreign'].update({'served': None}),
           DIAG_NONDET)
    expect('planted-never-journaled', lambda record:
           record['journals']['driven'].update({'adoptions': []}),
           DIAG_NONDET)
    expect('teardown-refused', lambda record:
           record['teardown'].update(
               {'errors': ['foreign: docker rm failed']}),
           DIAG_NONDET)
    expect('seat-leftover', lambda record:
           record['teardown'].update({'absent': False}), DIAG_NONDET)
    return slipped


def scenario_pending_source_pull(ctx):
    """Exercise the transient checkpoint-pull recovery contract on the
    leg's scratch field: launch a born-active into pending against
    the frozen field, stage a --standby tracking it inside the
    pending window and a same-shape control tracking the deployed
    incumbent, thaw the field so the source claims and serves, and
    assert the windowed standby converges to the verdict the field's
    claim state owns — tracking or orphaned — inside the documented
    bound, without a restart, with pulls landing on the same endpoint
    and the journaled convergence evidence standing, while the
    control converges identically and the deployed pair stays
    undisturbed. Two consecutive passes must produce identical
    digests."""
    case = Case(
        'pending-source-pull',
        'A standby tracking a pending source converges once it '
        'serves, without a restart',
        'a --standby launched inside its source\'s pending window '
        'converges to the non-degraded verdict the field\'s claim '
        'state owns inside the documented bound once the source '
        'serves — never a latched fetch failure a restart must clear '
        '— pulls land on the same endpoint, the durable and served '
        'journals carry the convergence evidence inside one process '
        'lifetime, a same-shape control tracking the deployed '
        'incumbent converges identically, the deployed pair stays '
        'undisturbed, the staging is swept, and two passes produce '
        'identical digests')
    try:
        missing = [key for key in (
            'start_born_field', 'pause_born_field',
            'unpause_born_field', 'stop_born_field',
            'start_born_controller', 'stop_born_controller',
            'born_controller_state', 'plant_remote')
            if ctx.get(key) is None]
        if missing:
            return case.finish('inconclusive', 'the run context '
                               'carries no pending-source staging '
                               'levers: ' + ', '.join(missing))
        if not all(ctx.get(seat) for seat in BORN_SEATS_USED):
            return case.finish('inconclusive', 'the run context '
                               'carries no published monitor for the '
                               'born seats')
        journals = ctx.get('journal_files') or {}
        if not all(journals.get(seat) for seat in BORN_SEATS_USED):
            return case.finish('inconclusive', 'the run context '
                               'carries no per-seat journal files — '
                               'the durable half of the audit cannot '
                               'run')

        digests = []
        for number in (1, 2):
            violations = {}

            def note(key, diagnostic, detail):
                violations.setdefault(key, (diagnostic, detail))

            record, evidence = _pull_pass(ctx, number)
            evidence['record'] = record
            if not evidence.get('inconclusive'):
                _judge_pull(record, note)
            digest = _digest_pull(record, violations)
            evidence['digest'] = dict(digest)
            evidence['violations'] = {
                key: diagnostic for key, (diagnostic, _)
                in violations.items()}
            ref = save_evidence(
                ctx['evidence_dir'],
                'pending-source-pull-pass-' + str(number)
                + '.json', evidence)
            case.evidence('file', ref,
                          'pending-source-pull pass '
                          + str(number) + ' — the staged pending '
                          'window, the served and journaled '
                          'convergence verdicts, the pull probes, '
                          'the pair checks, and the normalized '
                          'digest')
            if evidence.get('inconclusive'):
                return case.finish('inconclusive',
                                   evidence['inconclusive'])
            if violations:
                name = DIAG_FAILED if any(
                    diagnostic == DIAG_FAILED
                    for diagnostic, _ in violations.values()) \
                    else DIAG_NONDET
                return case.finish(
                    'failed', name + ': ' + '; '.join(
                        detail for _, detail in
                        list(violations.values())[:4]))
            digests.append(digest)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', DIAG_NONDET + ': the two passes\' digests '
                'diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two pending-source pull passes, identical '
                     'digests: '
                     + json.dumps(digests[0], sort_keys=True))

        # The unchecked-diagnostic self-check: the judge replays each
        # planted negative it must name; a silent judge means the leg
        # can no longer catch what it names.
        slipped = _pull_self_check()
        if slipped:
            return case.finish('failed', DIAG_UNCHECKED
                               + ': planted negatives slipped the '
                               'leg’s own audits: '
                               + ', '.join(slipped))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
