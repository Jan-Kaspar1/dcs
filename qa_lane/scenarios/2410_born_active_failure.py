"""The born_active_failure leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the leg stages on the scenario seats 'revised'/'foreign'/
# 'driven' and its own scratch field — it needs doomed_startup_claim's
# foreign seat released and must be done before the revision legs take
# the 'revised' seat over.
RUNS_AFTER = frozenset({'scenario_doomed_startup_claim'})
RUNS_BEFORE = frozenset({'scenario_incompatible_revision',
                         'scenario_model_revision'})


# --------------------------------------------------------------------
# The born-active startup-failure contract — the per-revision lane
# evidence for decision 103 (#985's record, #1017's implementation,
# the deferred half of #816; WW-LCM-001, WW-OPS-003): every
# startup-failure class a launched active's deferred startup claim can
# meet settles to the recorded disposition — served and journaled —
# rather than wedging unreported or dying silently. The two QA
# findings the contract answers are replayed on the leg's own scratch
# sim-serve field, so the inductions never touch the deployed pair's
# plant or claim arbitration:
#
# - `launched-active-boot-requires-reachable-plant` (class
#   'field-unreachable'): the born pair launches while the field's
#   name resolves but nothing listens. The recorded response is the
#   pending-claim state, never an exit or a wedge: `role: standby`,
#   an honest sync verdict, `field_claim` absent while no probe has
#   answered, the gate closed (commands refuse `not_active`), driver
#   diagnostics `disconnected` carrying `last_error`, and the
#   journaled fenced stand-down `active → standby`. The first
#   answered field contact re-issues the deferred conditional grant:
#   the run walks `standby → promoting → active` and the declared
#   pair reconverges `tracking`.
#
# - `startup-claim-refusal-leaves-pair-unpaired` (classes
#   'claim-refused-declared' and 'claim-refused-undeclared'): a live
#   incumbent's claim refuses the launch's conditional grant. With a
#   declared `--peer` the run rejoins as the pair's standby —
#   journaled `field_claim_observed` naming the incumbent's token —
#   and converges `tracking` on its stream; without one, the process
#   exits nonzero naming the refusal and the `--standby` remedy. The
#   incumbent is never preempted either way.
#
# - 'claim-inconclusive' (class (c)): a frozen field answers attaches
#   but no claim verdict — the `Err` leg holds the same pending state
#   and re-issues the grant once per answered contact; the thawed
#   field's verdict resolves it.
#
# - 'peer-unreachable': the record's declared-peer edge — a refused
#   launch whose `--peer` names nothing reachable rejoins the pair
#   honestly: standby, claim observed held, commands refused, sync
#   reporting the pull miss forever — never an exit, never a
#   `tracking` it cannot hold.
#
# No class may wedge: a peer reporting a role it cannot hold — `active`
# or `promoting` without `field_claim: held`, or `tracking`/`orphaned`
# behind a dead or unowned source — or a pair left unpaired while the
# failure is classified inconclusive is a contract breach.
#
# Named diagnostics: born-active-failure-failed tags the contract
# clauses — a pending surface that is not the recorded one, a refused
# declared-peer launch that exits or never rejoins, an undeclared
# refusal that survives or exits unnamed, a pending run that never
# resolves on first contact, an incumbent the refusal disturbs, or any
# class leaving a wedge — and born-active-failure-nondeterministic
# tags the instability the contract does not answer for: a refused
# staging call, a starved watch, two passes whose digests diverge. A
# staged run that predates the contract — the born-active exits on an
# unreachable field, or a refused launch exits despite a declared
# pair — reports inconclusive, as do rigs that cannot stage the leg
# at all. The unchecked-diagnostic self-check replays the judge over
# planted negatives and reports born-active-failure-unchecked for any
# that slip through.

BORN_SETTLE = 45   # bound on each pending/rejoin/recovery wait
BORN_POLL = 0.4    # cadence polling a seat's monitor mid-stage
BORN_WATCH = 4     # reads the pending/degraded windows must span
COMMAND_POINT = 1000  # pump_station's declared writable bool in-point
DEAD_PEER = 'dcs-born-dead-peer:8082'  # the unresolvable --peer name
DIAG_FAILED = 'born-active-failure-failed'
DIAG_NONDET = 'born-active-failure-nondeterministic'
DIAG_UNCHECKED = 'born-active-failure-unchecked'

# The sync verdicts an honest standby may report — the pending state
# and the dead-peer rejoin both produce one of these; 'tracking' is
# only honest behind a field-owning source.
HONEST_SYNC = ('unsynchronized', 'degraded', 'orphaned', 'diverged',
               'reinitialized')


def _sync_kind(report):
    """The served StandbySync's variant name — 'unsynchronized' is a
    bare string, the rest are single-key objects."""
    sync = (report or {}).get('sync')
    if isinstance(sync, str):
        return sync
    if isinstance(sync, dict) and sync:
        return next(iter(sync))
    return None


def _born_role(ctx, seat):
    return _try_role(ctx, ctx[seat])


def _born_view(ctx, seat):
    """One read of a born seat's served surface: the RoleReport plus
    the snapshot's driver diagnostics and tick — the pending surface's
    evidence tuple."""
    report = _try_role(ctx, ctx[seat])
    if report is None:
        return None
    snapshot = _try_snapshot(ctx, ctx[seat]) or {}
    driver = ((snapshot.get('io_health') or {}).get('driver') or {})
    return {'role': report.get('role'),
            'sync': _sync_kind(report),
            'field_claim': report.get('field_claim'),
            'link': driver.get('link'),
            'last_error': driver.get('last_error'),
            'tick': snapshot.get('tick')}


def _born_command_refused(ctx, seat):
    """POST a declared writable command to the seat: the pending or
    rejoined run must receipt it `not_active` — the closed gate's
    served evidence. Returns the rejection reason's kind, or None on
    a different verdict."""
    try:
        _, receipt = http_json('POST', ctx[seat] + '/command', {
            'command': {'write_value': {'point': COMMAND_POINT,
                                        'kind': 'bool',
                                        'value': {'bool': True}}},
            'actor': 'qa-born-active'})
    except Exception:
        return None
    reason = ((receipt or {}).get('outcome', {}).get('rejected', {})
              .get('reason') or {})
    return next(iter(reason), None)


def _born_journal(ctx, seat):
    """The seat's durable journal records — its runner-owned
    --journal-file the born launch reset at launch."""
    path = (ctx.get('journal_files') or {}).get(seat)
    if not path or not Path(path).is_file():
        return []
    return _journal_entries(path)


def _born_events(ctx, seat, kind):
    """The `kind` event bodies the seat's durable journal carries."""
    out = []
    for item in _born_journal(ctx, seat):
        event = (item.get('entry') or {}).get('event') or {}
        if isinstance(event.get(kind), dict):
            out.append(event[kind])
    return out


def _born_stand_down(ctx, seat):
    """Whether the seat's journal carries the recorded pending/refusal
    stand-down — `role_changed` active → standby under the
    field-arbitration origin."""
    return any(event.get('from') == 'active'
               and event.get('to') == 'standby'
               and event.get('origin') == 'fenced'
               for event in _born_events(ctx, seat, 'role_changed'))


def _born_transition(ctx, seat, source, target):
    """Whether the seat's journal carries a `role_changed` from → to —
    the grant's landing walks standby → promoting → active."""
    return any(event.get('from') == source and event.get('to') == target
               for event in _born_events(ctx, seat, 'role_changed'))


def _born_claimant(ctx, seat):
    """The owner tokens the seat's journaled `field_claim_observed`
    records attribute — the refusal's named incumbent."""
    return sorted({event.get('claimant')
                   for event in _born_events(ctx, seat,
                                             'field_claim_observed')
                   if event.get('claimant') is not None})


def _born_state(ctx, seat):
    """The seat container's process verdict through the runner's
    read-only state lever."""
    state = ctx.get('born_controller_state')
    if state is None:
        return None
    try:
        return state(seat)
    except Exception:
        return None


def _wait_role(ctx, seat, match, deadline=None):
    """Poll the seat's /role until `match(report)` holds; returns the
    report or None."""
    return wait_for(
        lambda: (lambda report: report if report is not None
                 and match(report) else None)(_born_role(ctx, seat)),
        deadline or time.monotonic() + BORN_SETTLE,
        interval=BORN_POLL)


def _watch_pending(ctx, seat):
    """Collect the pending window's served views plus the process
    verdict: BORN_WATCH reads of /role + /snapshot while the seat is
    asked a command once — the watch proves the pending surface holds
    rather than exiting or flapping."""
    views = []
    refused = None
    deadline = time.monotonic() + BORN_SETTLE
    while len(views) < BORN_WATCH and time.monotonic() < deadline:
        view = _born_view(ctx, seat)
        if view is not None:
            views.append(view)
            if refused is None:
                refused = _born_command_refused(ctx, seat)
        time.sleep(BORN_POLL)
    state = _born_state(ctx, seat)
    return {'views': views, 'refused': refused,
            'running': (state or {}).get('running'),
            'exit': (state or {}).get('exit'),
            'absent': (state or {}).get('absent')}


def _class_unreachable(ctx, record, remote):
    """Class (a) — the `launched-active-boot-requires-reachable-plant`
    replay: launch the born pair against the silent field, collect the
    pending surface, serve the field, and collect the recovery."""
    cls = {'class': 'field-unreachable'}
    try:
        ctx['start_born_controller']('foreign', remote,
                                     standby='revised')
        launched = ctx['start_born_controller']('revised', remote,
                                                peer='foreign')
    except Exception as exc:
        cls['stage_error'] = str(exc)[:300]
        return cls
    cls['launch'] = launched
    pending = _watch_pending(ctx, 'revised')
    cls['pending'] = pending
    cls['peer_pending'] = _born_role(ctx, 'foreign')
    cls['stand_down'] = _born_stand_down(ctx, 'revised')
    if pending.get('running') is False or pending.get('exit'):
        # The pre-record shape: the launch died inside driver
        # assembly rather than standing pending.
        cls['pre_contract'] = True
        return cls
    if not pending['views']:
        cls['starved'] = True
        return cls
    try:
        ctx['start_born_field']('serving')
    except Exception as exc:
        cls['stage_error'] = str(exc)[:300]
        return cls
    cls['recovered'] = _wait_role(
        ctx, 'revised',
        lambda report: report.get('role') == 'active')
    cls['converged'] = _wait_role(
        ctx, 'foreign',
        lambda report: report.get('role') == 'standby'
        and _sync_kind(report) == 'tracking')
    cls['grant_journaled'] = (
        _born_transition(ctx, 'revised', 'standby', 'promoting')
        and _born_transition(ctx, 'revised', 'promoting', 'active'))
    return cls


def _class_refused_declared(ctx, record, incumbent):
    """Class (b) declared — the `startup-claim-refusal-leaves-pair-
    unpaired` replay's rejoin half: a born-active naming its pair
    member is refused by the live incumbent's claim and settles as
    the pair's tracking standby."""
    cls = {'class': 'claim-refused-declared'}
    before = _born_role(ctx, incumbent) or {}
    cls['incumbent_before'] = {'role': before.get('role'),
                               'tick': before.get('tick'),
                               'field_claim': before.get('field_claim')}
    try:
        launched = ctx['start_born_controller']('driven', record['remote'],
                                              peer=incumbent)
    except Exception as exc:
        cls['stage_error'] = str(exc)[:300]
        return cls
    cls['launch'] = launched
    cls['rejoined'] = _wait_role(
        ctx, 'driven',
        lambda report: report.get('role') == 'standby'
        and _sync_kind(report) in HONEST_SYNC + ('tracking',))
    cls['converged'] = _wait_role(
        ctx, 'driven',
        lambda report: report.get('role') == 'standby'
        and _sync_kind(report) == 'tracking')
    cls['refused'] = _born_command_refused(ctx, 'driven')
    cls['state'] = _born_state(ctx, 'driven')
    cls['stand_down'] = _born_stand_down(ctx, 'driven')
    cls['claimants'] = _born_claimant(ctx, 'driven')
    after = _born_role(ctx, incumbent) or {}
    cls['incumbent_after'] = {'role': after.get('role'),
                              'tick': after.get('tick'),
                              'field_claim': after.get('field_claim')}
    return cls


def _class_refused_undeclared(ctx, record, incumbent):
    """Class (b) undeclared — the refusal's exit half: a born-active
    declaring no pair has no rejoin target, so the recorded
    disposition is the nonzero exit naming the refusal and the
    --standby remedy."""
    cls = {'class': 'claim-refused-undeclared'}
    before = _born_role(ctx, incumbent) or {}
    cls['incumbent_before'] = {'role': before.get('role'),
                               'tick': before.get('tick'),
                               'field_claim': before.get('field_claim')}
    try:
        ctx['start_born_controller']('driven', record['remote'])
    except Exception as exc:
        cls['stage_error'] = str(exc)[:300]
        return cls
    state = wait_for(
        lambda: (lambda state: state
                 if state is not None and not state.get('running')
                 else None)(_born_state(ctx, 'driven')),
        time.monotonic() + BORN_SETTLE, interval=BORN_POLL)
    if state is None:
        state = _born_state(ctx, 'driven')
    cls['state'] = state
    after = _born_role(ctx, incumbent) or {}
    cls['incumbent_after'] = {'role': after.get('role'),
                              'tick': after.get('tick'),
                              'field_claim': after.get('field_claim')}
    return cls


def _class_peer_unreachable(ctx, record, incumbent):
    """The record's declared-peer edge: the refused launch rejoins
    honestly even when its declared --peer names nothing reachable —
    standby with the pull-miss verdict, never an exit and never a
    convergence it cannot hold."""
    cls = {'class': 'peer-unreachable'}
    before = _born_role(ctx, incumbent) or {}
    cls['incumbent_before'] = {'role': before.get('role'),
                               'tick': before.get('tick'),
                               'field_claim': before.get('field_claim')}
    try:
        ctx['start_born_controller']('driven', record['remote'],
                                     peer=DEAD_PEER)
    except Exception as exc:
        cls['stage_error'] = str(exc)[:300]
        return cls
    views = []
    deadline = time.monotonic() + BORN_SETTLE
    while len(views) < BORN_WATCH and time.monotonic() < deadline:
        report = _born_role(ctx, 'driven')
        if report is not None:
            views.append({'role': report.get('role'),
                          'sync': _sync_kind(report),
                          'field_claim': report.get('field_claim')})
        time.sleep(BORN_POLL)
    cls['views'] = views
    cls['refused'] = _born_command_refused(ctx, 'driven')
    cls['state'] = _born_state(ctx, 'driven')
    cls['stand_down'] = _born_stand_down(ctx, 'driven')
    cls['claimants'] = _born_claimant(ctx, 'driven')
    after = _born_role(ctx, incumbent) or {}
    cls['incumbent_after'] = {'role': after.get('role'),
                              'tick': after.get('tick'),
                              'field_claim': after.get('field_claim')}
    return cls


def _class_inconclusive(ctx, record):
    """Class (c) — the verdict-free claim: the incumbent's seat is
    freed (dropping its claim's live holder) and the field frozen, so
    the launch's attach lands but the grant request never answers —
    the `Err` leg holds pending until the thaw lets the re-issued
    conditional grant land."""
    cls = {'class': 'claim-inconclusive'}
    try:
        ctx['stop_born_controller']('revised')
        ctx['pause_born_field']()
        ctx['start_born_controller']('revised', record['remote'])
    except Exception as exc:
        cls['stage_error'] = str(exc)[:300]
        return cls
    pending = _watch_pending(ctx, 'revised')
    cls['pending'] = pending
    cls['stand_down'] = _born_stand_down(ctx, 'revised')
    if pending.get('running') is False or pending.get('exit'):
        cls['pre_contract'] = True
        return cls
    if not pending['views']:
        cls['starved'] = True
        return cls
    try:
        ctx['unpause_born_field']()
    except Exception as exc:
        cls['stage_error'] = str(exc)[:300]
        return cls
    cls['recovered'] = _wait_role(
        ctx, 'revised',
        lambda report: report.get('role') == 'active'
        and report.get('field_claim') == 'held')
    cls['grant_journaled'] = (
        _born_transition(ctx, 'revised', 'standby', 'promoting')
        and _born_transition(ctx, 'revised', 'promoting', 'active'))
    return cls


def _born_pass(ctx, number, incumbent):
    """One pass over the recorded startup-failure classes on the leg's
    scratch field: silence → pending → serve → incumbency → the
    refused, dead-peer, and frozen-claim classes in order. Returns
    (record, evidence); the judge replays the record."""
    record = {'pass': number, 'classes': {}}
    evidence = {'pass': number, 'classes': {}}
    try:
        field = ctx['start_born_field']('silent')
    except Exception as exc:
        record['stage_error'] = str(exc)[:300]
        return record, evidence
    record['remote'] = field['remote']
    record['classes']['field-unreachable'] = \
        _class_unreachable(ctx, record, field['remote'])
    if record['classes']['field-unreachable'].get('pre_contract'):
        evidence['inconclusive'] = (
            'the born-active exited on the unreachable field rather '
            'than standing pending — the staged run predates the '
            'born-active startup-failure contract')
        return record, evidence
    if not record['classes']['field-unreachable'].get('recovered'):
        return record, evidence
    # The recovered born-active holds the scratch field's claim — the
    # live incumbent every refusal class launches against.
    record['classes']['claim-refused-declared'] = \
        _class_refused_declared(ctx, record, incumbent)
    if (record['classes']['claim-refused-declared'].get('state')
            or {}).get('running') is False:
        # A refused launch that exits despite its declared pair is the
        # pre-record disposition — the pair-rejoin half is absent.
        evidence['inconclusive'] = (
            'the declared-pair refusal exited instead of rejoining '
            'as the pair\'s standby — the staged run predates the '
            'born-active startup-failure contract')
        return record, evidence
    record['classes']['claim-refused-undeclared'] = \
        _class_refused_undeclared(ctx, record, incumbent)
    record['classes']['peer-unreachable'] = \
        _class_peer_unreachable(ctx, record, incumbent)
    record['classes']['claim-inconclusive'] = \
        _class_inconclusive(ctx, record)
    if record['classes']['claim-inconclusive'].get('pre_contract'):
        evidence['inconclusive'] = (
            'the verdict-free claim exited instead of holding '
            'pending — the staged run predates the born-active '
            'startup-failure contract')
        return record, evidence
    return record, evidence


def _pending_ok(pending):
    """The recorded pending-claim surface on one seat: standby, an
    honest sync verdict, no observed claim, a refused command, the
    link-down diagnostic, a live process, and a tick that moves."""
    views = pending.get('views') or []
    if not views:
        return False
    ticks = [view.get('tick') for view in views
             if isinstance(view.get('tick'), int)]
    return all(view.get('role') == 'standby'
               and view.get('sync') in HONEST_SYNC
               and view.get('field_claim') is None
               and view.get('link') == 'disconnected'
               and view.get('last_error')
               for view in views) \
        and pending.get('refused') == 'not_active' \
        and pending.get('running') is True \
        and len(set(ticks)) > 1


def _incumbent_held(before, after):
    """The incumbent was never disturbed: still active, still holding
    the claim, its tick advancing across the refusal window."""
    return after.get('role') == 'active' \
        and after.get('field_claim') == 'held' \
        and isinstance(before.get('tick'), int) \
        and isinstance(after.get('tick'), int) \
        and after['tick'] > before['tick']


def _judge_born(record, note):
    """Audit one pass's record — replayable, so the self-check can hand
    it planted negatives. `note(key, diagnostic, detail)` records each
    clause the record violates: DIAG_FAILED tags the contract clauses —
    a pending surface that is not the recorded one, a refused launch
    that exits or never rejoins, an undeclared refusal that survives or
    exits unnamed, a pending run that never resolves on first contact,
    an incumbent the refusal disturbs, a dead-peer rejoin that converges
    or dies, any wedge — and DIAG_NONDET tags the instability the
    contract does not answer for: a refused staging call, a starved
    watch. An aborted stage ends the audit where the pass ended."""
    def failed(key, detail):
        note(key, DIAG_FAILED, detail)

    def nondet(key, detail):
        note(key, DIAG_NONDET, detail)

    if record.get('stage_error') is not None:
        nondet('stage', 'the born-field staging never completed: '
               + str(record['stage_error']))
        return
    classes = record.get('classes') or {}
    incumbent_token = record.get('incumbent_token')

    cls = classes.get('field-unreachable') or {}
    if cls.get('stage_error') is not None:
        nondet('unreachable-stage', 'the unreachable-field staging '
               'never completed: ' + str(cls['stage_error']))
    elif cls.get('pre_contract'):
        pass  # classified inconclusive by the pass itself
    elif 'pending' not in cls:
        pass  # the pass aborted before this class staged
    elif cls.get('starved') or not cls['pending'].get('views'):
        nondet('unreachable-watch', 'the pending watch collected no '
               'served verdicts — the monitor never answered inside '
               'the bound')
    else:
        if not _pending_ok(cls.get('pending') or {}):
            failed('pending-surface', 'the unreachable-field launch '
                   'did not hold the recorded pending surface — '
                   'standby, an honest sync, no observed claim, a '
                   'refused command, the disconnected-link '
                   'diagnostic, a live process, a moving tick: '
                   + json.dumps(cls.get('pending'))[:300])
        peer = cls.get('peer_pending')
        if peer is not None and not (peer.get('role') == 'standby'
                                     and _sync_kind(peer)
                                     in HONEST_SYNC):
            failed('peer-pending', 'the pending run\'s pair member '
                   'reported a role it cannot hold: '
                   + json.dumps(peer)[:200])
        if not cls.get('stand_down'):
            failed('stand-down', 'the pending stand-down never '
                   'journaled — no role_changed active → standby '
                   'under the field-arbitration origin')
        if not cls.get('recovered'):
            failed('unreachable-recovery', 'the first answered field '
                   'contact never landed the deferred grant — the '
                   'pending run never reached active: '
                   + json.dumps(cls.get('pending'))[:200])
        elif (cls.get('recovered') or {}).get('field_claim') != 'held':
            failed('unreachable-claim', 'the recovered run reports '
                   'active without field_claim held: '
                   + json.dumps(cls.get('recovered'))[:200])
        if cls.get('recovered') and not cls.get('converged'):
            failed('pair-reconverge', 'the declared pair never '
                   'reconverged tracking behind the recovered owner')
        if cls.get('recovered') and not cls.get('grant_journaled'):
            failed('grant-journal', 'the deferred grant\'s landing '
                   'never journaled standby → promoting → active')

    cls = classes.get('claim-refused-declared') or {}
    if cls.get('stage_error') is not None:
        nondet('declared-stage', 'the refused-declared staging never '
               'completed: ' + str(cls['stage_error']))
    elif 'rejoined' in cls or 'converged' in cls:
        if not cls.get('rejoined'):
            failed('rejoin', 'the declared-pair refusal never served '
                   'the rejoined standby — the refusal left the pair '
                   'unpaired')
        else:
            rejoined = cls['rejoined']
            if rejoined.get('field_claim') != 'held':
                failed('rejoin-claim', 'the rejoined standby does not '
                       'observe the incumbent\'s held claim: '
                       + json.dumps(rejoined)[:200])
            if cls.get('refused') != 'not_active':
                failed('rejoin-commands', 'the rejoined standby '
                       'admitted or mislabeled a command: '
                       + str(cls.get('refused')))
            if not cls.get('stand_down'):
                failed('rejoin-journal', 'the refused launch\'s '
                       'stand-down never journaled')
            if incumbent_token is not None \
                    and cls.get('claimants') != [incumbent_token]:
                failed('rejoin-claimant', 'the refusal\'s observed '
                       'claimant is not the incumbent\'s token '
                       + str(incumbent_token) + ': '
                       + json.dumps(cls.get('claimants')))
            if not cls.get('converged'):
                failed('rejoin-converge', 'the rejoined standby never '
                       'converged tracking on the declared peer')
        if not _incumbent_held(cls.get('incumbent_before') or {},
                               cls.get('incumbent_after') or {}):
            failed('incumbent', 'the live incumbent was disturbed by '
                   'the refused launch — the conditional grant must '
                   'never preempt it: '
                   + json.dumps(cls.get('incumbent_after'))[:200])

    cls = classes.get('claim-refused-undeclared') or {}
    if cls.get('stage_error') is not None:
        nondet('undeclared-stage', 'the refused-undeclared staging '
               'never completed: ' + str(cls['stage_error']))
    elif 'state' in cls:
        state = cls.get('state') or {}
        if state.get('running') or not state.get('exit'):
            failed('undeclared-exit', 'the undeclared refusal did not '
                   'exit nonzero — a claim refused with no declared '
                   'pair has no recorded rejoin: '
                   + json.dumps(state)[:200])
        elif 'no --peer was declared' not in (state.get('logs') or ''):
            failed('undeclared-named', 'the undeclared refusal\'s exit '
                   'did not name the refusal and the --standby '
                   'remedy: ' + (state.get('logs') or '')[-200:])
        if not _incumbent_held(cls.get('incumbent_before') or
                               {'tick': 0},
                               cls.get('incumbent_after') or {}):
            failed('undeclared-incumbent', 'the incumbent was '
                   'disturbed by the undeclared refusal')

    cls = classes.get('peer-unreachable') or {}
    if cls.get('stage_error') is not None:
        nondet('peer-stage', 'the peer-unreachable staging never '
               'completed: ' + str(cls['stage_error']))
    elif 'views' in cls:
        views = cls.get('views') or []
        state = cls.get('state') or {}
        if state.get('running') is False or state.get('exit'):
            failed('peer-exit', 'the dead-peer rejoin exited — '
                   'an unreachable --peer is a pull miss, never '
                   'a startup error: ' + json.dumps(state)[:200])
        elif not views:
            nondet('peer-watch', 'the dead-peer watch collected no '
                   'served verdicts')
        else:
            if not all(view.get('role') == 'standby'
                       and view.get('sync') in HONEST_SYNC
                       and view.get('field_claim') == 'held'
                       for view in views):
                failed('peer-honest', 'the dead-peer rejoin reported '
                       'a verdict it cannot hold: '
                       + json.dumps(views)[:300])
            if cls.get('refused') != 'not_active':
                failed('peer-commands', 'the dead-peer standby '
                       'admitted or mislabeled a command: '
                       + str(cls.get('refused')))
            if not cls.get('stand_down'):
                failed('peer-journal', 'the dead-peer rejoin\'s '
                       'stand-down never journaled')
            if incumbent_token is not None \
                    and cls.get('claimants') != [incumbent_token]:
                failed('peer-claimant', 'the dead-peer rejoin\'s '
                       'observed claimant is not the incumbent\'s '
                       'token: ' + json.dumps(cls.get('claimants')))
        if not _incumbent_held(cls.get('incumbent_before') or
                               {'tick': 0},
                               cls.get('incumbent_after') or {}):
            failed('peer-incumbent', 'the incumbent was disturbed by '
                   'the dead-peer rejoin')

    cls = classes.get('claim-inconclusive') or {}
    if cls.get('stage_error') is not None:
        nondet('inconclusive-stage', 'the inconclusive-claim staging '
               'never completed: ' + str(cls['stage_error']))
    elif cls.get('pre_contract'):
        pass  # classified inconclusive by the pass itself
    elif 'pending' not in cls:
        pass  # the pass aborted before this class staged
    elif cls.get('starved') or not cls['pending'].get('views'):
        nondet('inconclusive-watch', 'the inconclusive-claim pending '
               'watch collected no served verdicts')
    else:
        pending = cls.get('pending') or {}
        # The frozen field's attach stands: the served surface is the
        # same standby/refused-command/no-claim verdict, but the link
        # may read connected — attach succeeded, the verdict never
        # came.
        views = pending.get('views') or []
        if not all(view.get('role') == 'standby'
                   and view.get('sync') in HONEST_SYNC
                   and view.get('field_claim') is None
                   for view in views) \
                or pending.get('refused') != 'not_active' \
                or pending.get('running') is not True:
            failed('inconclusive-surface', 'the verdict-free claim '
                   'did not hold the pending surface — never exit, '
                   'never rejoin: ' + json.dumps(pending)[:300])
        if not cls.get('stand_down'):
            failed('inconclusive-journal', 'the inconclusive claim\'s '
                   'stand-down never journaled')
        if not cls.get('recovered'):
            failed('inconclusive-recovery', 'the thawed field\'s '
                   'answered contact never landed the re-issued '
                   'grant')
        elif not cls.get('grant_journaled'):
            failed('inconclusive-grant', 'the re-issued grant\'s '
                   'landing never journaled standby → promoting → '
                   'active')


def _digest_born(record, violations):
    """The pass's normalized verdict record — identical across clean
    passes; each field carries the class's recorded disposition only
    while no violation names it."""
    def clean(*keys):
        return not any(key in violations for key in keys)
    return {
        'field-unreachable': 'pending-then-active'
            if clean('unreachable-stage', 'unreachable-watch',
                     'pending-surface', 'peer-pending', 'stand-down',
                     'unreachable-recovery', 'unreachable-claim',
                     'pair-reconverge', 'grant-journal')
            else 'defect',
        'claim-refused-declared': 'rejoined-tracking'
            if clean('declared-stage', 'rejoin', 'rejoin-claim',
                     'rejoin-commands', 'rejoin-journal',
                     'rejoin-claimant', 'rejoin-converge')
            else 'defect',
        'claim-refused-undeclared': 'exited-named'
            if clean('undeclared-stage', 'undeclared-exit',
                     'undeclared-named', 'undeclared-incumbent')
            else 'defect',
        'peer-unreachable': 'standby-degraded'
            if clean('peer-stage', 'peer-watch', 'peer-honest',
                     'peer-commands', 'peer-exit', 'peer-journal',
                     'peer-claimant')
            else 'defect',
        'claim-inconclusive': 'pending-then-active'
            if clean('inconclusive-stage', 'inconclusive-watch',
                     'inconclusive-surface', 'inconclusive-journal',
                     'inconclusive-recovery', 'inconclusive-grant')
            else 'defect',
        'incumbent': 'undisturbed'
            if clean('incumbent', 'undeclared-incumbent',
                     'peer-incumbent')
            else 'disturbed'}


def _self_check():
    """The leg's unchecked-diagnostic self-test: replay the judge over
    each planted negative — every recorded failure class's wrong
    disposition must name DIAG_FAILED; the instability shapes must
    report DIAG_NONDET. Returns the negative names the judge let
    through."""
    slipped = []

    def clean_record():
        return {
            'remote': 'dcs-hw-qa-born-plant:9003',
            'incumbent_token': 424245,
            'classes': {
                'field-unreachable': {
                    'pending': {
                        'views': [{'role': 'standby',
                                   'sync': 'unsynchronized',
                                   'field_claim': None,
                                   'link': 'disconnected',
                                   'last_error': 'refused',
                                   'tick': 1},
                                  {'role': 'standby',
                                   'sync': 'unsynchronized',
                                   'field_claim': None,
                                   'link': 'disconnected',
                                   'last_error': 'refused',
                                   'tick': 2}],
                        'refused': 'not_active', 'running': True,
                        'exit': None, 'absent': False},
                    'peer_pending': {'role': 'standby'},
                    'stand_down': True,
                    'recovered': {'role': 'active',
                                  'field_claim': 'held'},
                    'converged': {'role': 'standby'},
                    'grant_journaled': True},
                'claim-refused-declared': {
                    'incumbent_before': {'role': 'active', 'tick': 10,
                                         'field_claim': 'held'},
                    'rejoined': {'role': 'standby',
                                 'field_claim': 'held'},
                    'converged': {'role': 'standby'},
                    'refused': 'not_active',
                    'state': {'running': True, 'exit': None},
                    'stand_down': True, 'claimants': [424245],
                    'incumbent_after': {'role': 'active', 'tick': 20,
                                        'field_claim': 'held'}},
                'claim-refused-undeclared': {
                    'state': {'running': False, 'exit': 1,
                              'logs': 'no --peer was declared, so '
                                      'there is no pair to rejoin'},
                    'incumbent_after': {'role': 'active', 'tick': 30,
                                        'field_claim': 'held'}},
                'peer-unreachable': {
                    'views': [{'role': 'standby',
                               'sync': 'unsynchronized',
                               'field_claim': 'held'}],
                    'refused': 'not_active',
                    'state': {'running': True, 'exit': None},
                    'stand_down': True, 'claimants': [424245],
                    'incumbent_after': {'role': 'active', 'tick': 40,
                                        'field_claim': 'held'}},
                'claim-inconclusive': {
                    'pending': {
                        'views': [{'role': 'standby',
                                   'sync': 'unsynchronized',
                                   'field_claim': None,
                                   'link': 'connected',
                                   'last_error': 'timed out',
                                   'tick': 1},
                                  {'role': 'standby',
                                   'sync': 'unsynchronized',
                                   'field_claim': None,
                                   'link': 'connected',
                                   'last_error': 'timed out',
                                   'tick': 2}],
                        'refused': 'not_active', 'running': True,
                        'exit': None, 'absent': False},
                    'stand_down': True,
                    'recovered': {'role': 'active',
                                  'field_claim': 'held'},
                    'grant_journaled': True}}}

    def expect(name, mutate, diagnostic=DIAG_FAILED):
        record = clean_record()
        mutate(record)
        found = {}
        _judge_born(record,
                    lambda key, diag, detail:
                    found.setdefault(key, diag))
        if diagnostic not in found.values():
            slipped.append(name)

    def pending(mutate):
        return lambda record: mutate(
            record['classes']['field-unreachable'])

    # The doctored negatives the issue names — the wrong class
    # response asserted per class: a pending surface that is not the
    # recorded one, a refused launch that never rejoins or converges,
    # an undeclared refusal that survives or exits unnamed, a dead-
    # peer rejoin that converges or dies, a pending run that never
    # resolves, an incumbent the refusal disturbs.
    expect('pending-reports-active', pending(lambda cls:
           cls['pending']['views'][0].update({'role': 'active',
                                              'field_claim': 'held'})))
    expect('pending-claims-held', pending(lambda cls:
           cls['pending']['views'][0].update(
               {'field_claim': 'held'})))
    expect('pending-admits-command', pending(
           lambda cls: cls['pending'].update({'refused': None})))
    expect('pending-link-healthy', pending(lambda cls:
           cls['pending']['views'][0].update({'link': 'connected'})))
    expect('pending-tick-stalled', pending(lambda cls:
           cls['pending']['views'][1].update({'tick': 1})))
    expect('pending-exited', pending(
           lambda cls: cls['pending'].update({'running': False})))
    expect('stand-down-unjournaled', pending(
           lambda cls: cls.update({'stand_down': False})))
    expect('grant-never-lands', pending(
           lambda cls: cls.update({'recovered': None})))
    expect('grant-unjournaled', pending(
           lambda cls: cls.update({'grant_journaled': False})))
    expect('pair-never-reconverges', pending(
           lambda cls: cls.update({'converged': None})))
    expect('rejoin-never-served', lambda record:
           record['classes']['claim-refused-declared']
           .update({'rejoined': None}))
    expect('rejoin-never-converges', lambda record:
           record['classes']['claim-refused-declared']
           .update({'converged': None}))
    expect('rejoin-unjournaled', lambda record:
           record['classes']['claim-refused-declared']
           .update({'stand_down': False}))
    expect('rejoin-wrong-claimant', lambda record:
           record['classes']['claim-refused-declared']
           .update({'claimants': [424246]}))
    expect('undeclared-survives', lambda record:
           record['classes']['claim-refused-undeclared']
           ['state'].update({'running': True, 'exit': None}))
    expect('undeclared-exit-unnamed', lambda record:
           record['classes']['claim-refused-undeclared']
           ['state'].update({'logs': 'claim refused'}))
    expect('incumbent-demoted', lambda record:
           record['classes']['claim-refused-declared']
           ['incumbent_after'].update({'role': 'standby',
                                       'field_claim': None}))
    expect('dead-peer-converges', lambda record:
           record['classes']['peer-unreachable']['views'][0]
           .update({'sync': 'tracking'}))
    expect('dead-peer-exits', lambda record:
           record['classes']['peer-unreachable']['state']
           .update({'running': False, 'exit': 1}))
    expect('inconclusive-exits', lambda record:
           record['classes']['claim-inconclusive']['pending']
           .update({'running': False, 'exit': 1}))
    expect('inconclusive-never-resolves', lambda record:
           record['classes']['claim-inconclusive']
           .update({'recovered': None}))
    # The instability shapes must report nondeterministic: a refused
    # staging call, a starved watch.
    expect('stage-refused', lambda record:
           record.update({'stage_error': 'docker run failed'}),
           DIAG_NONDET)
    expect('watch-starved', pending(
           lambda cls: cls['pending'].update({'views': []})),
           DIAG_NONDET)
    return slipped


def _born_teardown(ctx):
    """Best-effort teardown: every born seat and the scratch field —
    a clean pass leaves nothing standing, and an aborted pass gets the
    same sweep so later legs see free seats and a free name."""
    for lever, seat in ((ctx.get('stop_born_controller'), seat)
                        for seat in ('revised', 'foreign', 'driven')):
        if lever is None:
            continue
        try:
            lever(seat)
        except Exception:
            pass
    try:
        if ctx.get('stop_born_field') is not None:
            ctx['stop_born_field']()
    except Exception:
        pass


def scenario_born_active_failure(ctx):
    """Exercise each born-active startup-failure class decision 103
    records on the leg's own scratch sim-serve field: launch the born
    pair against the silent field and hold the pending-claim surface
    until the first answered contact lands the deferred grant; refuse
    a born-active's conditional claim under the incumbent's hold —
    rejoining as the declared pair's tracking standby where --peer is
    declared, exiting nonzero with the named refusal where it is not;
    hold pending again through a frozen field's verdict-free claim
    until the thaw answers it; and keep the dead-peer rejoin honest —
    no class may leave a wedge: a peer reporting a role it cannot
    hold, or a pair left unpaired while the failure reads
    inconclusive. Two consecutive passes must produce identical
    digests."""
    case = Case(
        'born-active-failure',
        'Born-active startup failures settle to the recorded '
        'dispositions',
        'each recorded startup-failure class — the unreachable field, '
        'the refused claim with and without a declared pair, the '
        'verdict-free claim, and the unreachable declared peer — '
        'disposes its launch exactly as decision 103 records: pending '
        'standby under a closed gate with the journaled stand-down, '
        'rejoin as the pair\'s tracking standby, or the named nonzero '
        'exit; served and journaled evidence lands per class, no '
        'incumbent is disturbed, no peer wedges or reports a role it '
        'cannot hold, and two passes produce identical digests')
    try:
        missing = [key for key in (
            'start_born_field', 'pause_born_field',
            'unpause_born_field', 'stop_born_field',
            'start_born_controller', 'stop_born_controller',
            'born_controller_state') if ctx.get(key) is None]
        if missing:
            return case.finish('inconclusive', 'the run context '
                               'carries no born-active staging '
                               'levers: ' + ', '.join(missing))
        if not all(ctx.get(seat) for seat in
                   ('revised', 'foreign', 'driven')):
            return case.finish('inconclusive', 'the run context '
                               'carries no published monitor for the '
                               'born seats')
        journals = ctx.get('journal_files') or {}
        if not all(journals.get(seat) for seat in
                   ('revised', 'foreign', 'driven')):
            return case.finish('inconclusive', 'the run context '
                               'carries no per-seat journal files — '
                               'the durable half of the audit cannot '
                               'run')
        tokens = ctx.get('plant_owner') or {}
        incumbent = 'revised'

        digests = []
        for number in (1, 2):
            violations = {}

            def note(key, diagnostic, detail):
                violations.setdefault(key, (diagnostic, detail))

            try:
                record, evidence = _born_pass(ctx, number, incumbent)
            finally:
                # Each pass ends with the rig swept — the seats and
                # the scratch field removed so the next pass and the
                # legs behind this one start cold, and so no leftover
                # incumbent trips the refuse-to-replace guard.
                _born_teardown(ctx)
            record['incumbent_token'] = tokens.get(incumbent)
            evidence['record'] = record
            if not evidence.get('inconclusive'):
                _judge_born(record, note)
            digest = _digest_born(record, violations)
            evidence['digest'] = dict(digest)
            evidence['violations'] = {
                key: diagnostic for key, (diagnostic, _)
                in violations.items()}
            ref = save_evidence(
                ctx['evidence_dir'],
                'born-active-failure-pass-' + str(number)
                + '.json', evidence)
            case.evidence('file', ref,
                          'born-active-failure pass '
                          + str(number) + ' — the staged failure '
                          'classes, the served and journaled '
                          'verdicts, the incumbent checks, and '
                          'the normalized digest')
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
        case.observe('two born-active failure passes, identical '
                     'digests: '
                     + json.dumps(digests[0], sort_keys=True))

        # The unchecked-diagnostic self-check: the judge replays each
        # planted negative it must name; a silent judge means the leg
        # can no longer catch what it names.
        slipped = _self_check()
        if slipped:
            return case.finish('failed', DIAG_UNCHECKED
                               + ': planted negatives slipped the '
                               'leg’s own audits: '
                               + ', '.join(slipped))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
