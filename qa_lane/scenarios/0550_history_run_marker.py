"""The history_run_marker acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the run-marker leg joins the restart legs' window — it
# reuses the runner's restart actions and the journal files those
# legs audit, so it lands behind the source-restart leg — and shares
# the launch-layout window before the tune case's a->b switch.
RUNS_AFTER = frozenset({'scenario_source_restart'})
RUNS_BEFORE = frozenset({'scenario_parameter_tune_carryover'})


# --------------------------------------------------------------------
# The served-history lifetime contract (WW-OPS-002's history clause,
# WW-FND-004's gap-honesty clause) — the #884/#886 workspace fixes
# pinned in crates/dcs-monitor/tests/history.rs and journal_file.rs,
# exercised here on the deployed pair where the restart is a real
# container death and the journal file's run count is real: every
# served PointHistory envelope stamps the producing process lifetime's
# `run` ordinal — the same counter the journal file's run_boundary
# markers carry — including on an empty `samples` answer, and history
# `seq` rides the run's tick domain rather than a per-process append
# count. A --state-file resume continues the axis while the `run` bump
# names the new lifetime; a cold start restarts the axis,
# distinguishable through `run` even while a held `since` cursor still
# filters every restarted sample out — a cursor consumer comparing
# `run` across polls reads the change as a restarted axis, never a
# phantom-idle stream. The leg holds one since-cursor on a served
# history point across each restart form: the tracking peer's warm
# restart must serve the bumped run on every answer and continue the
# axis past the cursor, and the field owner's cold restart must answer
# the held cursor with empty samples stamped with the bumped run while
# the fresh axis climbs below it — the journal file gaining a
# run-boundary marker per lifetime, the cold one's at tick 0. Named
# diagnostics history-run-marker-failed for a contract miss and
# history-run-marker-nondeterministic when the passes or the record
# disagree with themselves; inconclusive when the rig is unreachable
# or predates the marker.

MARKER_SETTLE = 45       # bound on the pair's settle and role restore
MARKER_RETURN = 60       # bound on the restarted monitor's return
MARKER_POLL = 0.3        # held-cursor and promotion-watch cadence
MARKER_ANSWERS = 4       # post-restart cursor answers each leg collects
MARKER_READ_CAP = 48     # bound on the held-cursor watch's reads
MARKER_SLACK = 8         # persist-lag slack on a resumed axis
MARKER_JOURNAL = 15      # bound on the run-boundary marker landing


def _marker_pull(ctx, base, point, since):
    """One held-cursor /history read: (run, seqs) for the point's served
    envelope, or None on a dropped read or an answer carrying no entry
    for the point — one lost poll, never the leg's verdict."""
    try:
        _, body = http_json('GET', base + '/history?point=' + str(point)
                            + '&since=' + str(since))
    except Exception:
        return None
    if not isinstance(body, list):
        return None
    for entry in body:
        if isinstance(entry, dict) and entry.get('point') == point:
            seqs = [row.get('seq') for row in entry.get('samples') or []]
            return entry.get('run'), seqs
    return None


def _marker_axis(ctx, base, point):
    """The point's full served axis — (run, seqs) at since=0, the
    whole retained window the restart verdicts diff."""
    return _marker_pull(ctx, base, point, 0)


def _marker_boundaries(path):
    """The run_boundary markers a --journal-file records — the file's
    own lifetime count, the ordinal the served `run` shares — or None
    when the file cannot be read."""
    try:
        items = _journal_entries(path)
    except (OSError, ValueError):
        return None
    return [item['run_boundary'] for item in items
            if 'run_boundary' in item]


def _axis_shape(seqs):
    """'empty' | 'ascending' | the violation — the served seq axis's
    ordering verdict."""
    if not seqs:
        return 'empty'
    if any(not isinstance(seq, int) or isinstance(seq, bool)
           for seq in seqs):
        return 'noninteger'
    if seqs != sorted(seqs) or len(set(seqs)) != len(seqs):
        return 'disordered'
    return 'ascending'


def _served_run(run):
    """The lifetime ordinal a served envelope carries — a positive int,
    or None on a producer predating the marker contract."""
    if not isinstance(run, int) or isinstance(run, bool) or run < 1:
        return None
    return run


def _marker_pass(ctx, number, owner, peer, point):
    """One restart pass: the tracking peer's warm restart under a held
    since-cursor, then the field owner's cold restart under its own —
    the run bump and the tick-domain verdicts on both, then the launch
    roles restored. Returns (digest, violations, evidence): digest is
    the pass's normalized verdict record, identical across clean
    passes; violations is {key: (diagnostic, detail)} in first-seen
    order. A lost restart action or a monitor that never returns
    raises — the rig-side failures the scenario reports inconclusive.
    """
    violations = {}
    evidence = {'pass': number, 'owner': owner, 'peer': peer,
                'point': point}
    digest = {'resume_run': 'unseen', 'resume_axis': 'unseen',
              'cold_run': 'unseen', 'cold_axis': 'unseen',
              'cold_cursor': 'unseen', 'roles': 'unrestored'}

    def note(key, diagnostic, detail):
        violations.setdefault(key, (diagnostic, detail))

    def failed(key, detail):
        note(key, 'history-run-marker-failed', detail)

    owner_base, peer_base = ctx[owner], ctx[peer]
    journals = ctx.get('journal_files') or {}

    # The settled gate: the launch layout the pass's restore owes —
    # the field owner holding and the tracked peer behind it.
    if _pair_active(ctx) != owner \
            or _tracking_standby(ctx, peer) is None:
        failed('settle', 'the pair never settled — ' + owner
               + ' holds no active role with ' + peer
               + ' tracking behind it')
        return None, violations, evidence

    # ---- the resume half: a warm restart of the tracking peer ----
    # The held cursor: the point's last served seq before the restart.
    # Across the peer's down window every post-restart answer must
    # stamp the new lifetime's run — empty `samples` included — and
    # the axis must continue the persisted tick domain.
    leg = {}
    evidence['resume'] = leg
    before = _marker_axis(ctx, peer_base, point)
    sibling0 = _marker_axis(ctx, owner_base, point)
    if before is None:
        raise ConnectionError('the tracked peer\'s /history stopped '
                              'serving the watched point')
    run0 = _served_run(before[0])
    if run0 is None:
        failed('baseline', 'the tracked peer\'s history envelope '
               'carries no lifetime ordinal: ' + str(before))
        return None, violations, evidence
    cursor = before[1][-1] if before[1] else 0
    marks0 = _marker_boundaries(journals[peer])
    if marks0 is None:
        raise ConnectionError('the tracked peer\'s journal file '
                              'cannot be read')
    leg['before'] = {'run': run0, 'cursor': cursor,
                     'journal_runs': len(marks0),
                     'sibling_run': _served_run((sibling0 or (None,))[0])}
    if len(marks0) != run0:
        failed('baseline-journal', 'the tracked peer serves run '
               + str(run0) + ' but its journal file records '
               + str(len(marks0)) + ' lifetimes — the served marker '
               'is not the file\'s run count')
    expected = run0 + 1
    try:
        ctx['restart_controller'](peer)
    except Exception as exc:
        raise ConnectionError('the restart action never completed: '
                              + str(exc)[:300])

    answers = []
    runs = set()
    filled = False
    deadline = time.monotonic() + MARKER_RETURN + MARKER_SETTLE
    while time.monotonic() < deadline \
            and len(answers) < MARKER_READ_CAP:
        pulled = _marker_pull(ctx, peer_base, point, cursor)
        if pulled is None:
            time.sleep(MARKER_POLL)
            continue
        run, seqs = pulled
        answers.append({'run': run, 'samples': len(seqs)})
        runs.add(run)
        if run != expected:
            break       # a wrong marker is terminal evidence
        if seqs:
            filled = True
            if len(answers) >= MARKER_ANSWERS:
                break
        time.sleep(MARKER_POLL)
    leg['answers'] = answers
    if not answers:
        raise ConnectionError('the restarted monitor never returned')
    if runs != {expected}:
        if len(runs) > 1:
            note('resume-flap', 'history-run-marker-nondeterministic',
                 'the served run moved inside the resumed lifetime: '
                 + json.dumps(sorted(
                     runs, key=lambda r: (r is None, r))))
        else:
            failed('resume-run', 'the held since-cursor saw run '
                   + json.dumps(sorted(runs, key=str))
                   + ' instead of the new lifetime ' + str(expected)
                   + ' — a phantom-idle stream')
    else:
        digest['resume_run'] = 'advanced'
    if not filled:
        failed('resume-starved', 'the held cursor at seq '
               + str(cursor) + ' never saw a post-restart sample — '
               'the resumed axis never continued')

    # The untouched peer's marker must not move: `run` is per-process
    # lifetime, never a line-wide counter.
    sibling1 = _marker_axis(ctx, owner_base, point)
    leg['sibling_after'] = _served_run((sibling1 or (None,))[0])
    if sibling0 is not None and sibling1 is not None \
            and sibling0[0] != sibling1[0]:
        failed('sibling-run', 'the untouched peer\'s served run moved '
               'during the tracked peer\'s restart: '
               + str(sibling0[0]) + ' -> ' + str(sibling1[0]))

    # The axis verdict: a --state-file resume continues the tick
    # domain, so the fresh ring's first seq rejoins the held cursor —
    # a restarted axis starting near the floor is the cold-start shape
    # this leg forbids here.
    axis = _marker_axis(ctx, peer_base, point)
    leg['axis'] = {'run': (axis or (None,))[0],
                   'head': (axis or (None, []))[1][:8] if axis else None}
    if axis is None:
        raise ConnectionError('the restarted monitor stopped serving '
                              '/history')
    run_ax, seqs_ax = axis
    shape = _axis_shape(seqs_ax)
    if _served_run(run_ax) != expected:
        failed('resume-envelope', 'the full history answer carries '
               'run ' + str(run_ax) + ' — not the resumed lifetime '
               + str(expected))
    if shape != 'ascending':
        failed('resume-axis', 'the resumed run\'s seq axis is '
               + shape + ': ' + json.dumps(seqs_ax[:12]))
    elif seqs_ax[0] >= cursor - MARKER_SLACK:
        digest['resume_axis'] = 'continued'
    else:
        failed('resume-axis', 'the resumed run\'s seq axis restarted '
               'at ' + str(seqs_ax[0]) + ' below the held cursor '
               + str(cursor) + ' — a --state-file resume must '
               'continue the tick domain')

    # The durable count: the journal file's run_boundary markers are
    # the ordinal the envelope shares — the resumed lifetime lands one
    # marker at the restored tick.
    def journaled():
        marks = _marker_boundaries(journals[peer])
        return marks if marks and len(marks) >= expected else None

    marks1 = wait_for(journaled, time.monotonic() + MARKER_JOURNAL,
                      interval=MARKER_POLL)
    leg['journal'] = marks1
    if marks1 is None or len(marks1) != expected:
        failed('resume-journal', 'the tracked peer\'s journal file '
               'never recorded its ' + str(expected)
               + 'th lifetime: ' + json.dumps(marks1)[:200])
    elif not isinstance((marks1[-1] or {}).get('tick'), int) \
            or marks1[-1]['tick'] < cursor - MARKER_SLACK:
        failed('resume-journal', 'the resumed lifetime\'s boundary '
               'marker carries tick '
               + str((marks1[-1] or {}).get('tick'))
               + ' — not the restored tick domain the resume owes')

    # ---- the cold half: a cold restart of the field owner ----
    # The held cursor sits above the fresh domain's floor: every
    # answer while the restarted axis climbs below it carries empty
    # samples stamped with the bumped run — never the old lifetime's
    # phantom-idle stream.
    leg = {}
    evidence['cold'] = leg
    before = _marker_axis(ctx, owner_base, point)
    if before is None:
        raise ConnectionError('the field owner\'s /history stopped '
                              'serving the watched point')
    run_a0 = _served_run(before[0])
    if run_a0 is None:
        failed('cold-baseline', 'the field owner\'s history envelope '
               'carries no lifetime ordinal: ' + str(before))
        return None, violations, evidence
    cursor_a = before[1][-1] if before[1] else 0
    marks_a0 = _marker_boundaries(journals[owner])
    if marks_a0 is None:
        raise ConnectionError('the field owner\'s journal file '
                              'cannot be read')
    leg['before'] = {'run': run_a0, 'cursor': cursor_a,
                     'journal_runs': len(marks_a0)}
    if len(marks_a0) != run_a0:
        failed('cold-baseline-journal', 'the field owner serves run '
               + str(run_a0) + ' but its journal file records '
               + str(len(marks_a0)) + ' lifetimes')
    if cursor_a <= MARKER_ANSWERS:
        raise ConnectionError('the field owner\'s seq axis sits at '
                              + str(cursor_a) + ' — the held cursor '
                              'leaves no window for a restarted axis '
                              'to hide behind')
    expected_a = run_a0 + 1
    # The standby's armed failover budget in seconds — a promotion
    # past it is the documented bound on the outage, not a defect;
    # one inside it is the spurious promotion the contract forbids.
    budget = (ctx.get('failover_misses') or 0) * 0.1
    stopped_at = time.monotonic()
    try:
        ctx['cold_restart_controller'](owner)
    except Exception as exc:
        raise ConnectionError('the cold-restart action never '
                              'completed: ' + str(exc)[:300])

    answers = []
    runs = set()
    promoted = []
    deadline = time.monotonic() + MARKER_RETURN + MARKER_SETTLE
    while time.monotonic() < deadline \
            and len(answers) < MARKER_ANSWERS:
        report = _try_role(ctx, peer_base)
        if (report or {}).get('role') in ('promoting', 'active'):
            promoted.append({'report': report,
                             'elapsed': time.monotonic()
                             - stopped_at})
        pulled = _marker_pull(ctx, owner_base, point, cursor_a)
        if pulled is None:
            time.sleep(MARKER_POLL)
            continue
        run, seqs = pulled
        answers.append({'run': run, 'samples': len(seqs)})
        runs.add(run)
        if run != expected_a:
            break       # a wrong marker is terminal evidence
        time.sleep(MARKER_POLL)
    leg['answers'] = answers
    leg['promoted'] = promoted
    if not answers:
        raise ConnectionError('the cold-restarted monitor never '
                              'returned')
    if promoted:
        elapsed = promoted[0]['elapsed']
        if budget and elapsed > budget:
            raise ConnectionError('the tracked peer promoted '
                                  + str(elapsed)[:5]
                                  + 's into the outage — past the '
                                  'armed failover budget of '
                                  + str(budget) + 's, the documented '
                                  'bound rather than a defect')
        failed('promoted', 'the tracked peer reported '
               + str(promoted[0]['report'].get('role'))
               + ' while the cold-restarted controller was down — '
               'a spurious promotion inside the armed failover '
               'budget: '
               + json.dumps(promoted[0]['report'])[:300])
    if runs != {expected_a}:
        if len(runs) > 1:
            note('cold-flap', 'history-run-marker-nondeterministic',
                 'the served run moved inside the cold lifetime: '
                 + json.dumps(sorted(
                     runs, key=lambda r: (r is None, r))))
        else:
            failed('cold-run', 'the held since-cursor saw run '
                   + json.dumps(sorted(runs, key=str))
                   + ' instead of the new lifetime ' + str(expected_a)
                   + ' — a phantom-idle stream')
    else:
        digest['cold_run'] = 'advanced'
        empty = [a for a in answers if not a['samples']]
        if answers[0]['samples'] or len(empty) < 2:
            failed('cold-cursor', 'the held cursor saw '
                   + str(len(empty)) + ' empty answers across the '
                   'restart — the restarted axis never hid behind '
                   'seq ' + str(cursor_a))
        else:
            digest['cold_cursor'] = 'marked-empty'

    axis = _marker_axis(ctx, owner_base, point)
    leg['axis'] = {'run': (axis or (None,))[0],
                   'head': (axis or (None, []))[1][:8] if axis else None}
    if axis is None:
        raise ConnectionError('the cold-restarted monitor stopped '
                              'serving /history')
    run_ax, seqs_ax = axis
    shape = _axis_shape(seqs_ax)
    if _served_run(run_ax) != expected_a:
        failed('cold-envelope', 'the full history answer carries run '
               + str(run_ax) + ' — not the cold lifetime '
               + str(expected_a))
    if shape != 'ascending':
        failed('cold-axis', 'the cold run\'s seq axis is ' + shape
               + ': ' + json.dumps(seqs_ax[:12]))
    elif seqs_ax[0] < cursor_a:
        digest['cold_axis'] = 'restarted'
    else:
        failed('cold-axis', 'the cold run\'s seq axis continued at '
               + str(seqs_ax[0]) + ' — a cold start must open a new '
               'tick domain the run bump distinguishes from the held '
               'cursor ' + str(cursor_a))

    def journaled_cold():
        marks = _marker_boundaries(journals[owner])
        return marks if marks and len(marks) >= expected_a else None

    marks_a1 = wait_for(journaled_cold,
                        time.monotonic() + MARKER_JOURNAL,
                        interval=MARKER_POLL)
    leg['journal'] = marks_a1
    if marks_a1 is None or len(marks_a1) != expected_a:
        failed('cold-journal', 'the field owner\'s journal file '
               'never recorded its ' + str(expected_a)
               + 'th lifetime: ' + json.dumps(marks_a1)[:200])
    elif (marks_a1[-1] or {}).get('tick') != 0:
        failed('cold-journal', 'the cold lifetime\'s boundary marker '
               'carries tick '
               + str((marks_a1[-1] or {}).get('tick'))
               + ' — the restart was not cold')

    # The launch roles the cases behind this one meet: the cold peer's
    # startup claim holds the field and the tracked peer reconverges
    # onto the regressed stream.
    settled = wait_for(
        lambda: (_pair_active(ctx) == owner or None)
                and _tracking_standby(ctx, peer),
        time.monotonic() + MARKER_SETTLE, interval=MARKER_POLL)
    evidence['restored'] = bool(settled)
    if settled:
        digest['roles'] = 'restored'
    else:
        failed('restore', 'the pair never settled back to the launch '
               'roles — ' + owner + ' active, ' + peer + ' tracking')
    evidence['digest'] = dict(digest)
    evidence['violations'] = {key: diagnostic
                              for key, (diagnostic, _)
                              in violations.items()}
    return digest, violations, evidence


def scenario_history_run_marker(ctx):
    """Exercise the served /history run-marker contract across real
    container restarts: the tracking peer's --state-file resume bumps
    `run` and continues the tick-domain seq axis, and the field
    owner's cold restart bumps `run` while the restarted axis hides
    behind a held since-cursor — every answer stamped, empty pages
    included."""
    case = Case(
        'history-run-marker',
        'The served /history run marker survives a restart',
        'with the deployed pair settled and a journaled point '
        'accumulating samples, a since-cursor held across the '
        'tracking peer\'s restart_controller action sees the '
        'envelope\'s run advance to the new lifetime ordinal while '
        'the seq axis continues the persisted tick domain, and a '
        'cursor held across the field owner\'s '
        'cold_restart_controller action sees the bumped run on '
        'empty-samples answers while the restarted axis climbs below '
        'it — each peer\'s journal file recording one run-boundary '
        'marker per lifetime, the cold one at tick 0 — and the pair '
        'restores its launch roles; two consecutive passes produce '
        'identical digests')
    try:
        for action in ('restart_controller',
                       'cold_restart_controller'):
            if ctx.get(action) is None:
                return case.finish('inconclusive', 'the run context '
                                   'carries no ' + action + ' action '
                                   '— the restart induction has no '
                                   'documented seam')
        journals = ctx.get('journal_files') or {}
        if journals.get('active') is None \
                or journals.get('standby') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no journal-file paths for the '
                               'pair — the file-side run count cannot '
                               'be audited')
        for name in ('active', 'standby'):
            try:
                _role(ctx, ctx[name])
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s '
                                   'monitor is unreachable: '
                                   + str(exc)[:200])
        deadline = time.monotonic() + MARKER_SETTLE
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=MARKER_POLL)
        if owner is None:
            return case.finish('failed', 'no peer reports '
                               'role=active')
        if owner != 'active':
            return case.finish('inconclusive', 'the field owner is '
                               + owner + ' — the leg\'s cold-restart '
                               'restore assumes the launch layout '
                               'where ctrl-a owns the field')
        peer = 'standby'
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=MARKER_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the resume leg '
                               'has no peer to restart')

        # The contract surface: a served point accumulating samples
        # whose envelope stamps the lifetime ordinal — a missing or
        # zero `run` on any served entry is a rig predating the
        # marker contract.
        baselines = {}
        point = None
        for name in (owner, peer):
            try:
                _, body = http_json('GET', ctx[name]
                                    + '/history?since=0')
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s '
                                   '/history never answered: '
                                   + str(exc)[:200])
            if not isinstance(body, list):
                return case.finish('inconclusive', name + '\'s '
                                   '/history answered a non-list '
                                   'payload — the rig predates the '
                                   'history surface')
            baselines[name] = body
            for entry in body:
                if not isinstance(entry, dict):
                    continue
                if _served_run(entry.get('run')) is None:
                    return case.finish(
                        'inconclusive', name + '\'s served history '
                        'envelopes carry no lifetime ordinal — the '
                        'rig predates the run-marker contract: '
                        + json.dumps(entry)[:200])
                if point is None and entry.get('samples'):
                    point = entry.get('point')
        if point is None:
            return case.finish('inconclusive', 'the pair serves no '
                               'history point accumulating samples')
        ref = save_evidence(ctx['evidence_dir'],
                            'history-run-marker-baseline.json',
                            {'owner': owner, 'peer': peer,
                             'point': point,
                             'runs': {name: [entry.get('run')
                                             for entry in body
                                             if isinstance(entry, dict)
                                             and entry.get('point')
                                             == point]
                                      for name, body
                                      in baselines.items()}})
        case.evidence('file', ref, 'the settled pair\'s served run '
                      'ordinals on the watched point')
        case.observe('watching point ' + str(point) + ': ' + owner
                     + ' owns the field, ' + peer + ' tracks')
        digests = []
        try:
            for number in (1, 2):
                digest, violations, evidence = _marker_pass(
                    ctx, number, owner, peer, point)
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'history-run-marker-pass-' + str(number) + '.json',
                    evidence)
                case.evidence('file', ref, 'run-marker pass '
                              + str(number) + ' — the held-cursor '
                              'answers across both restart forms, the '
                              'journal run-boundary audit, and the '
                              'normalized digest')
                if violations or digest is None:
                    diagnostic = 'history-run-marker-failed' \
                        if digest is None or any(
                            name == 'history-run-marker-failed'
                            for name, _ in violations.values()) \
                        else 'history-run-marker-nondeterministic'
                    return case.finish(
                        'failed', diagnostic + ': ' + '; '.join(
                            detail for _, detail in
                            list(violations.values())[:4]))
                digests.append(digest)
        finally:
            # The launch layout for the cases behind this one — a
            # clean pass restores it by construction, and the field
            # owner's own startup claim heals a promoted peer; an
            # aborted pass gets the same bounded settle wait.
            restored = wait_for(
                lambda: (_pair_active(ctx) == 'active' or None)
                        and _tracking_standby(ctx, 'standby'),
                time.monotonic() + MARKER_SETTLE,
                interval=MARKER_POLL)
            if not restored:
                case.observe('cleanup: the pair never settled back '
                             'to the launch roles')
        if digests[0] != digests[1]:
            return case.finish(
                'failed', 'history-run-marker-nondeterministic: the '
                'two passes\' digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two restart passes, identical digests')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
