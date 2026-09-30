"""The deferred_startup_refusal leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the leg stages on the born seats 'revised'/'foreign'/
# 'driven' and freezes the deployed pair's plant — it needs
# born_active_failure's seats released and must be done before the
# revision legs take the 'revised' seat over.
RUNS_AFTER = frozenset({'scenario_born_active_failure'})
RUNS_BEFORE = frozenset({'scenario_incompatible_revision',
                         'scenario_model_revision'})


# --------------------------------------------------------------------
# The deferred startup-claim refusal contract — the per-revision lane
# evidence for #1301's fix (WW-LCM-001 continuity, the born-active
# startup-failure contract's deferred half): a conditional startup
# grant refused at the first *answered* field contact — rather than at
# activation — must take the identical `settle_activation` disposition
# the boot-time verdict takes. A born-active launched while the field
# is frozen goes pending; the thaw lets the re-issued ask meet the
# live incumbent's standing claim, and the refused run settles:
#
# - undeclared (no --peer): the run has no pair to rejoin — a nonzero
#   exit naming the refusal and the standby remedy, inside the
#   documented bound. The defect the contract answers left that seat
#   living forever as an unpaired standby — unpromotable, tracking
#   nothing, indistinguishable from a legitimate standby on the
#   monitor — while a fresh launcher took the field. The durable
#   `startup_claim_refused` journal record proves the pending state
#   settled under a verdict; the `field_claim_observed` record names
#   the incumbent that refused it.
#
# - declared (--peer): the run keeps standing — rejoined as the pair's
#   standby — and converges tracking on the declared member. The
#   refusal path must not swallow the declared-pair case.
#
# - a legitimate `--standby` launch in the same frozen-field shape
#   still converges and tracks: it never ran a startup claim, so the
#   refusal path must not touch it.
#
# The deployed pair is the incumbent: `docker pause` on its plant
# freezes the field server without severing the holder's claim — a
# born-active's attach completes into the listener's backlog but no
# claim verdict answers — and `unpause` drains the backlog so the
# incumbent's queued re-arms land before any post-thaw ask. The pair's
# run must stay undisturbed throughout: same field owner, claim held,
# ticks advancing — and every seat the leg stood is removed again.
#
# Named diagnostics: deferred-refusal-strand-failed tags the contract
# clauses — a deferred-refused seat that survives as a peerless
# standby, an exit that does not name the refusal or the remedy, a
# refusal that never journaled or names the wrong claimant, a declared
# pair the refusal swallowed, a --standby seat that failed to track, a
# disturbed incumbent — and deferred-refusal-strand-nondeterministic
# tags the instability the contract does not answer for: a refused
# staging call, a starved watch, two passes whose digests diverge. A
# staged run that predates the contract — the pending born-active
# exits before the field can answer, or the refused run keeps
# standing with the refusal journaled only as the observed claimant
# and no `startup_claim_refused` settle record — reports inconclusive.
# The unchecked-diagnostic self-check replays the judge over planted
# negatives and reports deferred-refusal-strand-unchecked for any
# that slip through.

DEFER_PENDING = 30   # bound on the pending surface serving while frozen
DEFER_SETTLE = 45    # bound on each post-thaw exit/converge wait
DEFER_POLL = 0.4     # cadence polling a seat's monitor mid-stage
DEFER_WATCH = 2      # pending reads each born-active must serve frozen
DIAG_FAILED = 'deferred-refusal-strand-failed'
DIAG_NONDET = 'deferred-refusal-strand-nondeterministic'
DIAG_UNCHECKED = 'deferred-refusal-strand-unchecked'

BORN_ACTIVES = ('driven', 'foreign')
BORN_SEATS_USED = ('driven', 'foreign', 'revised')
# The refusal text the settled launch names on stderr — the shared
# startup_claim_refused SwitchError's own detail — and the remedy half
# the pairless disposition appends.
REFUSAL_FRAGMENT = 'a live peer holds the field'
REMEDY_FRAGMENTS = ('no --peer was declared', 'standby')


def _seat_role(ctx, seat):
    return _try_role(ctx, ctx[seat])


def _seat_view(ctx, seat):
    """One read of a born seat's served surface: the RoleReport — the
    pending surface's evidence tuple."""
    report = _try_role(ctx, ctx[seat])
    if report is None:
        return None
    return {'role': report.get('role'),
            'sync': _seat_sync(report),
            'field_claim': report.get('field_claim'),
            'tick': report.get('tick')}


def _seat_sync(report):
    sync = (report or {}).get('sync')
    if isinstance(sync, str):
        return sync
    if isinstance(sync, dict) and sync:
        return next(iter(sync))
    return None


def _tracking(report):
    return (report or {}).get('role') == 'standby' \
        and _seat_sync(report) == 'tracking'


def _member_view(ctx, name):
    """One deployed member's served surface — the undisturbed-pair
    evidence: role, tick, and the observed field claim."""
    report = _try_role(ctx, ctx[name])
    if report is None:
        return None
    return {'role': report.get('role'),
            'tick': report.get('tick'),
            'field_claim': report.get('field_claim')}


def _seat_state(ctx, seat):
    state = ctx.get('born_controller_state')
    if state is None:
        return None
    try:
        return state(seat)
    except Exception:
        return None


def _seat_journal(ctx, seat):
    path = (ctx.get('journal_files') or {}).get(seat)
    if not path or not Path(path).is_file():
        return []
    return _journal_entries(path)


def _seat_events(ctx, seat, kind):
    out = []
    for item in _seat_journal(ctx, seat):
        event = (item.get('entry') or {}).get('event') or {}
        if isinstance(event.get(kind), dict):
            out.append(event[kind])
    return out


def _seat_evidence(ctx, seat):
    """The seat's journaled refusal evidence: whether the deferred
    settle's `startup_claim_refused` record landed and which owner
    tokens the observed-claimant records attribute."""
    return {'refusal_journaled':
            bool(_seat_events(ctx, seat, 'startup_claim_refused')),
            'claimants': sorted(
                {event.get('claimant')
                 for event in _seat_events(ctx, seat,
                                           'field_claim_observed')
                 if event.get('claimant') is not None})}


def _wait_tracking(ctx, seat, deadline=None):
    """The seat's report once it serves standby/tracking, else the
    last report (or None) when the bound passed."""
    return wait_for(
        lambda: (lambda report: report if _tracking(report)
                 else None)(_seat_role(ctx, seat)),
        deadline or time.monotonic() + DEFER_SETTLE,
        interval=DEFER_POLL)


def _wait_exit(ctx, seat, deadline=None):
    """The seat container's process verdict once it is no longer
    running, else its last state when the bound passed."""
    return wait_for(
        lambda: (lambda state: state
                 if state is not None
                 and (state.get('absent') or not state.get('running'))
                 else None)(_seat_state(ctx, seat)),
        deadline or time.monotonic() + DEFER_SETTLE,
        interval=DEFER_POLL)


def _watch_pending(ctx):
    """The frozen-field window's served views: every staged seat polled
    until both born-actives have stood DEFER_WATCH reads — the proof
    the conditional grant went pending rather than answering at
    activation. Ends early if a born-active exits while frozen: no
    verdict can land on an unanswered field, so an exit here is the
    pre-contract disposition."""
    views = {seat: [] for seat in BORN_SEATS_USED}
    states = {}
    deadline = time.monotonic() + DEFER_PENDING
    while time.monotonic() < deadline:
        for seat in BORN_SEATS_USED:
            view = _seat_view(ctx, seat)
            if view is not None:
                views[seat].append(view)
        early = False
        for seat in BORN_ACTIVES:
            state = _seat_state(ctx, seat)
            if state is not None:
                states[seat] = state
            if (states.get(seat) or {}).get('running') is False:
                early = True
        if early or all(len(views[seat]) >= DEFER_WATCH
                        for seat in BORN_ACTIVES):
            break
        time.sleep(DEFER_POLL)
    return {'views': views, 'states': states}


def _deferred_pass(ctx, number):
    """One pass over the deferred-refusal contract on the deployed
    pair's plant: freeze the field, launch the pairless and declared
    born-actives plus the --standby control behind the incumbent's
    standing claim, prove the pending surface, thaw, and collect each
    seat's settle plus the pair's undisturbedness. Returns (record,
    evidence); the judge replays the record."""
    record = {'pass': number}
    evidence = {'pass': number}
    incumbent = _pair_active(ctx)
    if incumbent is None:
        evidence['inconclusive'] = (
            'the deployed pair reports no field-owning member — '
            'the incumbent the deferred ask must race never settled')
        return record, evidence
    member = 'standby' if incumbent == 'active' else 'active'
    record['remote'] = ctx['plant_remote']
    record['incumbent'] = incumbent
    record['incumbent_token'] = (ctx.get('plant_owner')
                                 or {}).get(incumbent)
    record['incumbent_before'] = _member_view(ctx, incumbent)
    record['member_before'] = _member_view(ctx, member)
    if (record['incumbent_before'] or {}).get('field_claim') != 'held' \
            or (record['member_before'] or {}).get('role') != 'standby':
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
            ctx['pause_plant']()
            paused = True
        except Exception as exc:
            record['stage_error'] = str(exc)[:300]
            return record, evidence
        try:
            ctx['start_born_controller']('driven', record['remote'])
            ctx['start_born_controller']('foreign', record['remote'],
                                         peer=incumbent)
            ctx['start_born_controller']('revised', record['remote'],
                                         standby=incumbent)
        except Exception as exc:
            record['stage_error'] = str(exc)[:300]
            return record, evidence
        # The freeze's evidence: both born-actives serving the pending
        # standby surface while the field cannot answer, processes
        # still running — the grant was deferred, not refused at
        # activation.
        record['pending'] = _watch_pending(ctx)
        if any((record['pending']['states'].get(seat) or {})
               .get('running') is False
               for seat in BORN_ACTIVES):
            evidence['inconclusive'] = (
                'a born-active exited while the field was still '
                'frozen — no claim verdict can land unanswered, so '
                'the staged run predates the born-active pending '
                'contract the deferred refusal rides on')
            return record, evidence
        try:
            ctx['unpause_plant']()
            paused = False
        except Exception as exc:
            record['stage_error'] = str(exc)[:300]
            return record, evidence
        record['refused'] = _wait_exit(ctx, 'driven')
        record['rejoined'] = _wait_tracking(ctx, 'foreign')
        record['standby_report'] = _wait_tracking(ctx, 'revised')
        record['states'] = {seat: _seat_state(ctx, seat)
                            for seat in BORN_SEATS_USED}
        record['journals'] = {seat: _seat_evidence(ctx, seat)
                              for seat in BORN_SEATS_USED}
        record['incumbent_after'] = _member_view(ctx, incumbent)
        record['member_after'] = _member_view(ctx, member)
        # The pre-#1301 shape: the deferred refusal landed — the
        # observed-claimant record names the incumbent — but nothing
        # latched the verdict for the shell, so the pairless run keeps
        # standing as an unpaired standby with no startup_claim_refused
        # record. The contract's settle path is absent; the leg cannot
        # assert it.
        driven = record['journals'].get('driven') or {}
        state = record.get('refused') or {}
        if state.get('running') and not state.get('absent') \
                and driven.get('claimants') \
                and not driven.get('refusal_journaled'):
            evidence['inconclusive'] = (
                'the deferred-refused run kept standing with the '
                'refusal journaled only as the observed claimant — no '
                'startup_claim_refused settle record exists, so the '
                'staged run predates the deferred-refusal contract')
            return record, evidence
    finally:
        if paused:
            try:
                ctx['unpause_plant']()
            except Exception:
                pass
        for seat in BORN_SEATS_USED:
            try:
                ctx['stop_born_controller'](seat)
            except Exception:
                pass
    return record, evidence


def _incumbent_undisturbed(before, after):
    """The deployed incumbent was never disturbed: still active, still
    holding the field's claim, its tick advancing across the freeze."""
    return (after or {}).get('role') == 'active' \
        and (after or {}).get('field_claim') == 'held' \
        and isinstance((before or {}).get('tick'), int) \
        and isinstance((after or {}).get('tick'), int) \
        and after['tick'] > before['tick']


def _judge_deferred(record, note):
    """Audit one pass's record — replayable, so the self-check can hand
    it planted negatives. `note(key, diagnostic, detail)` records each
    clause the record violates: DIAG_FAILED tags the contract clauses —
    a deferred-refused seat stranded peerless, an unnamed or
    unjournaled refusal, a declared pair the refusal swallowed, a
    --standby seat that never tracked, a disturbed incumbent — and
    DIAG_NONDET tags the instability the contract does not answer for:
    a refused staging call, a starved pending watch."""
    def failed(key, detail):
        note(key, DIAG_FAILED, detail)

    def nondet(key, detail):
        note(key, DIAG_NONDET, detail)

    if record.get('stage_error') is not None:
        nondet('stage', 'the frozen-field staging never completed: '
               + str(record['stage_error']))
        return
    pending = record.get('pending') or {}
    views = pending.get('views') or {}
    states = pending.get('states') or {}
    for seat in BORN_ACTIVES:
        seat_views = views.get(seat) or []
        if not seat_views:
            nondet('pending-watch', 'the ' + seat + ' seat never '
                   'served the pending surface while the field stood '
                   'frozen — the deferral never staged')
        elif not all(view.get('role') == 'standby'
                     and view.get('field_claim') is None
                     for view in seat_views):
            failed('pending-surface', 'the ' + seat + ' seat\'s '
                   'pending surface reported a role it cannot hold — '
                   'standby with no observed claim is the deferred '
                   'grant\'s only honest report: '
                   + json.dumps(seat_views)[:300])
        if states.get(seat) is not None \
                and states[seat].get('running') is not True:
            failed('pending-exit', 'the ' + seat + ' seat was not '
                   'running at the thaw — the deferred ask belongs to '
                   'a live pending run')

    refused = record.get('refused') or {}
    journal = (record.get('journals') or {}).get('driven') or {}
    token = record.get('incumbent_token')
    if refused.get('absent'):
        nondet('refused-vanished', 'the deferred-refused seat\'s '
               'container vanished — no process verdict exists to '
               'audit')
    elif refused.get('running'):
        if journal.get('refusal_journaled'):
            failed('refused-stranded', 'the deferred-refused pairless '
                   'run kept standing after its refusal journaled — '
                   'the peerless standby wedge the contract exists to '
                   'prevent')
        else:
            failed('refused-unsettled', 'the deferred grant never '
                   'landed a verdict on the thawed field — the pending '
                   'run still stands unanswered past the bound')
    elif not refused.get('exit'):
        failed('refused-exit', 'the deferred-refused pairless run did '
               'not exit nonzero: ' + json.dumps(refused)[:200])
    else:
        logs = refused.get('logs') or ''
        if REFUSAL_FRAGMENT not in logs:
            failed('refusal-named', 'the deferred refusal\'s exit '
                   'never named the refusal: ' + logs[-200:])
        if not all(fragment in logs for fragment in REMEDY_FRAGMENTS):
            failed('remedy-named', 'the deferred refusal\'s exit '
                   'never named the standby remedy: ' + logs[-200:])
    if not journal.get('refusal_journaled') and not refused.get('running'):
        failed('refusal-journaled', 'the deferred settle never '
               'journaled startup_claim_refused — the pending state '
               'has no durable verdict record')
    if token is not None and journal.get('claimants') not in \
            (None, [token]):
        failed('refusal-claimant', 'the refusal\'s observed claimant '
               'is not the incumbent\'s token ' + str(token) + ': '
               + json.dumps(journal.get('claimants')))

    rejoined = record.get('rejoined')
    state = (record.get('states') or {}).get('foreign') or {}
    journal = (record.get('journals') or {}).get('foreign') or {}
    if state.get('running') is not True:
        failed('declared-exit', 'the declared-pair refusal exited — '
               'the deferred settle swallowed the rejoin the declared '
               '--peer records: ' + json.dumps(state)[:200])
    elif not _tracking(rejoined):
        failed('declared-unconverged', 'the deferred-refused declared '
               'seat never converged tracking on the incumbent: '
               + json.dumps(rejoined)[:200])
    elif rejoined.get('field_claim') != 'held':
        failed('declared-claim', 'the rejoined standby does not '
               'observe the incumbent\'s held claim: '
               + json.dumps(rejoined)[:200])
    if not journal.get('refusal_journaled'):
        failed('declared-journaled', 'the declared seat\'s deferred '
               'settle never journaled startup_claim_refused')
    if token is not None and journal.get('claimants') not in \
            (None, [token]):
        failed('declared-claimant', 'the declared seat\'s observed '
               'claimant is not the incumbent\'s token: '
               + json.dumps(journal.get('claimants')))

    standby = record.get('standby_report')
    state = (record.get('states') or {}).get('revised') or {}
    journal = (record.get('journals') or {}).get('revised') or {}
    if state.get('running') is not True:
        failed('standby-exit', 'the declared --standby seat exited — '
               'it never ran a startup claim for the refusal path to '
               'swallow: ' + json.dumps(state)[:200])
    elif not _tracking(standby):
        failed('standby-unconverged', 'the declared --standby seat '
               'never converged tracking on the incumbent: '
               + json.dumps(standby)[:200])
    if journal.get('refusal_journaled'):
        failed('standby-journaled', 'the declared --standby seat '
               'journaled a startup_claim_refused — it never issued '
               'a startup claim for a refusal to answer')

    if not _incumbent_undisturbed(record.get('incumbent_before'),
                                  record.get('incumbent_after')):
        failed('incumbent', 'the deployed pair\'s incumbent was '
               'disturbed — the deferred refusal must leave the '
               'standing claim\'s owner active, held, and advancing: '
               + json.dumps({'before': record.get('incumbent_before'),
                             'after': record.get('incumbent_after')}
                            )[:300])
    member = record.get('member_after') or {}
    if member.get('role') != 'standby':
        failed('member', 'the deployed pair\'s tracking member left '
               'standby — the staged refusal disturbed the pair\'s '
               'second member: ' + json.dumps(member)[:200])


def _digest_deferred(record, violations):
    """The pass's normalized verdict record — identical across clean
    passes; each field carries the recorded disposition only while no
    violation names it."""
    def clean(*keys):
        return not any(key in violations for key in keys)
    return {
        'pending-deferral': 'deferred'
            if clean('pending-watch', 'pending-surface', 'pending-exit')
            else 'defect',
        'deferred-undeclared': 'exited-named'
            if clean('refused-vanished', 'refused-stranded',
                     'refused-unsettled', 'refused-exit',
                     'refusal-named', 'remedy-named',
                     'refusal-journaled', 'refusal-claimant')
            else 'defect',
        'deferred-declared': 'rejoined-tracking'
            if clean('declared-exit', 'declared-unconverged',
                     'declared-claim', 'declared-journaled',
                     'declared-claimant')
            else 'defect',
        'declared-standby': 'tracking'
            if clean('standby-exit', 'standby-unconverged',
                     'standby-journaled')
            else 'defect',
        'deployed-pair': 'undisturbed'
            if clean('incumbent', 'member')
            else 'disturbed'}


def _deferred_self_check():
    """The leg's unchecked-diagnostic self-test: replay the judge over
    each planted negative — every recorded failure shape must name
    DIAG_FAILED; the instability shapes must report DIAG_NONDET.
    Returns the negative names the judge let through."""
    slipped = []

    def clean_record():
        pending_views = [{'role': 'standby', 'sync': 'unsynchronized',
                          'field_claim': None, 'tick': 1},
                         {'role': 'standby', 'sync': 'unsynchronized',
                          'field_claim': None, 'tick': 2}]
        return {
            'remote': 'dcs-hw-qa-1-plant:9001',
            'incumbent': 'active',
            'incumbent_token': 424243,
            'incumbent_before': {'role': 'active', 'tick': 10,
                                 'field_claim': 'held'},
            'member_before': {'role': 'standby', 'tick': 8,
                              'field_claim': 'held'},
            'pending': {'views': {
                            'driven': list(pending_views),
                            'foreign': list(pending_views),
                            'revised': [{'role': 'standby',
                                         'sync': 'tracking',
                                         'field_claim': None,
                                         'tick': 1}]},
                        'states': {
                            'driven': {'running': True, 'exit': None},
                            'foreign': {'running': True,
                                        'exit': None}}},
            'refused': {'running': False, 'exit': 1, 'absent': False,
                        'logs': 'error: a live peer holds the '
                                'field\'s write-ownership claim — '
                                'rejoin as a standby instead — no '
                                '--peer was declared, so there is no '
                                'pair to rejoin'},
            'rejoined': {'role': 'standby',
                         'sync': {'tracking': {'aligned': 42}},
                         'field_claim': 'held', 'tick': 3},
            'standby_report': {'role': 'standby',
                               'sync': {'tracking': {'aligned': 42}},
                               'field_claim': 'held', 'tick': 3},
            'states': {
                'driven': {'running': False, 'exit': 1},
                'foreign': {'running': True, 'exit': None},
                'revised': {'running': True, 'exit': None}},
            'journals': {
                'driven': {'refusal_journaled': True,
                           'claimants': [424243]},
                'foreign': {'refusal_journaled': True,
                            'claimants': [424243]},
                'revised': {'refusal_journaled': False,
                            'claimants': []}},
            'incumbent_after': {'role': 'active', 'tick': 30,
                                'field_claim': 'held'},
            'member_after': {'role': 'standby', 'tick': 20,
                             'field_claim': 'held'}}

    def expect(name, mutate, diagnostic=DIAG_FAILED):
        record = clean_record()
        mutate(record)
        found = {}
        _judge_deferred(record,
                        lambda key, diag, detail:
                        found.setdefault(key, diag))
        if diagnostic not in found.values():
            slipped.append(name)

    # The doctored negatives the issue names: the refused pairless
    # seat surviving as a standby, the unnamed exit, the declared
    # pair's swallowed rejoin, the --standby control's failure, the
    # deployed pair's disturbance, the wedge surfaces.
    expect('refused-seat-stranded', lambda record:
           record['refused'].update({'running': True, 'exit': None}))
    expect('refused-seat-never-settles', lambda record:
           (record['refused'].update({'running': True, 'exit': None}),
            record['journals']['driven']
            .update({'refusal_journaled': False})))
    expect('refused-exit-zero', lambda record:
           record['refused'].update({'exit': 0}))
    expect('refused-exit-unrefused', lambda record:
           record['refused'].update({'logs': 'error: no --peer was '
                                     'declared, so there is no pair '
                                     'to rejoin — rejoin as a standby '
                                     'instead'}))
    expect('refused-exit-no-remedy', lambda record:
           record['refused'].update({'logs': 'error: a live peer '
                                     'holds the field\'s '
                                     'write-ownership claim'}))
    expect('refusal-unjournaled', lambda record:
           record['journals']['driven']
           .update({'refusal_journaled': False}))
    expect('refusal-wrong-claimant', lambda record:
           record['journals']['driven']
           .update({'claimants': [424246]}))
    expect('declared-seat-exits', lambda record:
           record['states']['foreign']
           .update({'running': False, 'exit': 1}))
    expect('declared-never-tracks', lambda record:
           record.update({'rejoined': {'role': 'standby',
                                       'sync': 'unsynchronized'}}))
    expect('declared-claim-unobserved', lambda record:
           record['rejoined'].update({'field_claim': None}))
    expect('declared-unjournaled', lambda record:
           record['journals']['foreign']
           .update({'refusal_journaled': False}))
    expect('standby-seat-exits', lambda record:
           record['states']['revised']
           .update({'running': False, 'exit': 1}))
    expect('standby-never-tracks', lambda record:
           record.update({'standby_report': {'role': 'standby',
                                             'sync': 'degraded'}}))
    expect('standby-journals-refusal', lambda record:
           record['journals']['revised']
           .update({'refusal_journaled': True}))
    expect('incumbent-demoted', lambda record:
           record['incumbent_after'].update({'role': 'standby',
                                             'field_claim': None}))
    expect('incumbent-tick-stalled', lambda record:
           record['incumbent_after'].update({'tick': 10}))
    expect('member-promoted', lambda record:
           record['member_after'].update({'role': 'active'}))
    expect('pending-reports-active', lambda record:
           record['pending']['views']['driven'][0]
           .update({'role': 'active', 'field_claim': 'held'}))
    expect('pending-seat-exited', lambda record:
           record['pending']['states']['driven']
           .update({'running': False, 'exit': 1}))
    # The instability shapes must report nondeterministic: a refused
    # staging call, a starved pending watch, a vanished container.
    expect('stage-refused', lambda record:
           record.update({'stage_error': 'docker run failed'}),
           DIAG_NONDET)
    expect('pending-watch-starved', lambda record:
           record['pending']['views'].update({'driven': []}),
           DIAG_NONDET)
    expect('refused-container-vanished', lambda record:
           record['refused'].update({'running': False, 'exit': None,
                                     'absent': True}),
           DIAG_NONDET)
    return slipped


def scenario_deferred_startup_refusal(ctx):
    """Exercise the deferred startup-claim refusal contract against
    the deployed pair's plant: freeze the field so a born-active's
    conditional startup grant stands pending, let the incumbent's
    standing claim meet the re-issued ask at the thaw's first answered
    contact, and settle the refused run exactly as the boot-time
    verdict does — the pairless seat exits nonzero naming the refusal
    and the standby remedy instead of living forever as an unpaired
    standby, the declared-pair seat rejoins and converges tracking,
    and a declared --standby seat in the same shape still tracks.
    The deployed pair stays undisturbed throughout and every staged
    seat is removed afterward. Two consecutive passes must produce
    identical digests."""
    case = Case(
        'deferred-startup-refusal',
        'A deferred startup-claim refusal settles like the boot-time '
        'verdict',
        'a born-active whose conditional startup grant is refused at '
        'the first answered field contact exits nonzero inside the '
        'bound naming the refusal and the standby remedy when no pair '
        'was declared, rejoins as the pair\'s tracking standby when '
        'one was, and journals the startup_claim_refused settle '
        'record; a declared --standby seat in the same shape still '
        'converges and tracks, the deployed pair is undisturbed and '
        'restored afterward, and two passes produce identical '
        'digests')
    try:
        missing = [key for key in (
            'pause_plant', 'unpause_plant', 'plant_remote',
            'start_born_controller', 'stop_born_controller',
            'born_controller_state') if ctx.get(key) is None]
        if missing:
            return case.finish('inconclusive', 'the run context '
                               'carries no deferred-refusal staging '
                               'levers: ' + ', '.join(missing))
        if not all(ctx.get(seat) for seat in BORN_SEATS_USED):
            return case.finish('inconclusive', 'the run context '
                               'carries no published monitor for the '
                               'born seats')
        if not all(ctx.get(member) for member in ('active', 'standby')):
            return case.finish('inconclusive', 'the run context '
                               'carries no published monitor for the '
                               'deployed pair')
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

            record, evidence = _deferred_pass(ctx, number)
            evidence['record'] = record
            if not evidence.get('inconclusive'):
                _judge_deferred(record, note)
            digest = _digest_deferred(record, violations)
            evidence['digest'] = dict(digest)
            evidence['violations'] = {
                key: diagnostic for key, (diagnostic, _)
                in violations.items()}
            ref = save_evidence(
                ctx['evidence_dir'],
                'deferred-startup-refusal-pass-' + str(number)
                + '.json', evidence)
            case.evidence('file', ref,
                          'deferred-startup-refusal pass '
                          + str(number) + ' — the frozen-field '
                          'staging, the pending surface, each '
                          'seat\'s settle verdict and journal '
                          'evidence, the incumbent checks, and the '
                          'normalized digest')
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
        case.observe('two deferred-refusal passes, identical '
                     'digests: '
                     + json.dumps(digests[0], sort_keys=True))

        # The unchecked-diagnostic self-check: the judge replays each
        # planted negative it must name; a silent judge means the leg
        # can no longer catch what it names.
        slipped = _deferred_self_check()
        if slipped:
            return case.finish('failed', DIAG_UNCHECKED
                               + ': planted negatives slipped the '
                               'leg’s own audits: '
                               + ', '.join(slipped))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
