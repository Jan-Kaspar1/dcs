"""The remote_foreign_model acceptance leg — one module per leg of the
scenario schedule; see qa_lane/scenarios/__init__.py for the ordering
rule and the shared seam."""
from .common import *

# Ordering: the leg stages on the scenario seats 'revised'/'foreign'
# and the born legs' scratch field — it needs born_active_failure's
# seats released and must be done before the revision legs take the
# 'revised' seat over.
RUNS_AFTER = frozenset({'scenario_born_active_failure'})
RUNS_BEFORE = frozenset({'scenario_incompatible_revision',
                         'scenario_model_revision'})


# --------------------------------------------------------------------
# The remote-attachment foreign-model correspondence contract — the
# per-revision lane evidence for #1302's fix (the sim-tcp spec-builder's
# recorded rule applied to --remote: declared-point correspondence is
# probed so a plant that answers serving a different model never lets
# the claim land). The defect the contract answers: a born-active
# launched --remote against a plant serving a foreign model claimed it
# and went active — the pump_station run owning a dosing-skid field —
# surfacing only as thousands of failed_reads and configuration_fault
# points post-claim, while fencing the field's rightful owner out. On
# the fixed revision the miswired remote is a named startup failure —
# 'plant server at <addr> does not serve io point 120: ...' or '...
# serves io point 20 as Bool, the model declares Float' — before the
# claim ask ever runs; on a build whose probes deferred, the pending
# run's re-probes refuse ahead of every conditional grant — the named
# pending-with-mismatch verdict — and the field's claim arbitration
# still reports unclaimed.
#
# The leg replays the miswire on the born legs' scratch sim-serve
# field: start_born_field('foreign') launches the run's plant image on
# the config's foreign fixtures — the dosing skid, whose served point
# set shares the rig model's low channel ids but declares nothing at
# 120 and serves 20 as bool where the rig declares float — then the
# labeled born-active launch declares the rig model with --remote
# aimed at it. The verdict is the container's process evidence plus
# the field's own arbitration, probed inside the field container
# through the shipped dcs-plant-ctl (born_field_ctl): a fenced step
# means a claim stands, a stepped one means none does, and the plant
# tick's stillness means the refused run never drove the plant. The
# positive control repeats the same launch shape against a same-model
# scratch field — it must claim, go active, and scan. The deployed
# pair never enters the staging: the scratch field is a different
# container on a different claim token, and the leg asserts the pair's
# roles and scan are undisturbed before and after every pass. Two
# consecutive passes must produce identical outcome digests.
#
# Named diagnostics: foreign-model-claim-failed tags the contract
# clauses — the named verdict's launch landing the claim or an active
# role anyway, a refusal that exits unnamed on a field proven foreign,
# the foreign field claimed or stepped by the mismatched run, the
# probe convicting the same-model control, or a claimed control that
# never scans — foreign-model-claim-nondeterministic tags the
# instability the contract does not answer for — refused staging
# calls, an unread verdict or an unverdicted pending surface, a
# control launch that never reaches active, a moved or wedged pair,
# or two passes whose digests diverge.
# A rig that cannot stage the leg, and a staged revision predating the
# contract — the mismatched launch claiming and going active with no
# named verdict — report inconclusive. The unchecked-diagnostic
# self-check replays the judge over planted negatives and reports
# foreign-model-claim-unchecked for any that slip through.

FOREIGN_SETTLE = 45   # bound on the pair settling and each verdict wait
FOREIGN_POLL = 0.4    # cadence polling a seat's verdict mid-watch
FOREIGN_WATCH = 4     # served reads a pending surface must span
FOREIGN_FAILED = 'foreign-model-claim-failed'
FOREIGN_NONDET = 'foreign-model-claim-nondeterministic'
FOREIGN_UNCHECKED = 'foreign-model-claim-unchecked'

MISMATCH_SEAT = 'revised'  # the foreign-declaring launch's seat
CONTROL_SEAT = 'foreign'   # the same-model control launch's seat
FOREIGN_ABSENT_POINT = 120  # the rig declares it; the skid serves none
FOREIGN_KIND_POINT = 20     # float in the rig model, bool on the skid
RIG_KIND = 'float'          # the rig's declared kind at the kind point

# The correspondence verdict's signature text — the startup refusal
# names the served-vs-declared divergence and the deferred probe's
# detail carries the same wording under its own prefix.
MISMATCH_MARKERS = ('does not serve io point', 'serves io point',
                    'deferred assembly probe')


def _foreign_ctl(ctx, *args):
    """One dcs-plant-ctl invocation against the leg's scratch field
    through the runner's born_field_ctl seam — the same response-object
    shape _plant_ctl returns for the deployed plant, a refused request
    coming back error-shaped rather than raised. None when the seam or
    the field never answered."""
    run = ctx.get('born_field_ctl')
    if run is None:
        return None
    try:
        result = run(*args)
    except Exception:
        return None
    if result.returncode == 0:
        try:
            answer = json.loads(result.stdout)
        except ValueError:
            return None
        return answer if isinstance(answer, dict) else None
    return {'result': 'error',
            'error': {'kind': 'tool_failed',
                      'detail': (result.stderr or '').strip()[:400],
                      'exit': result.returncode}}


def _foreign_census(ctx):
    """The served point ids the field's own list answers — the census
    that corroborates the staging fixture's model, or None unanswered."""
    answer = _foreign_ctl(ctx, 'list')
    points = (answer or {}).get('points')
    if not isinstance(points, list):
        return None
    return sorted(entry.get('point') for entry in points
                  if isinstance(entry.get('point'), int)
                  and not isinstance(entry.get('point'), bool))


def _foreign_sample(ctx, point):
    """The kind the field serves `point` as — the sample value's wire
    kind on an answered read, 'unserved' when the field answers the
    point unknown, None/'unread' when the read never answered."""
    answer = _foreign_ctl(ctx, 'read', str(point))
    if not isinstance(answer, dict):
        return None
    sample = answer.get('sample')
    if isinstance(sample, dict):
        value = sample.get('value')
        if isinstance(value, dict) and value:
            return next(iter(value))
        return 'sampled'
    detail = str(((answer or {}).get('error') or {}).get('detail')
                 or '')
    return 'unserved' if 'unknown' in detail else 'unread'


def _foreign_ping(ctx):
    """The scratch field's own plant tick — the stepping evidence a
    driving owner produces; None when the field never answered."""
    answer = _foreign_ctl(ctx, 'ping')
    tick = (answer or {}).get('tick')
    if isinstance(tick, int) and not isinstance(tick, bool):
        return tick
    return None


def _foreign_claim(ctx):
    """The field's write-ownership verdict, probed by the tool's
    conditional `step`: 'unclaimed' — the tool's ensure_writer joined
    and released an ownerless field; 'claimed' — a standing claim
    fenced it; 'unread' — the field never answered."""
    answer = _foreign_ctl(ctx, 'step', '0')
    if not isinstance(answer, dict):
        return 'unread'
    if answer.get('result') == 'stepped':
        return 'unclaimed'
    detail = str((answer.get('error') or {}).get('detail') or '')
    if 'another attachment owns' in detail:
        return 'claimed'
    if 'no attachment owns' in detail:
        return 'unclaimed'
    return 'unread'


def _foreign_role(ctx, base):
    """The pass's normalized role evidence for one deployed member —
    role, scan tick, tracking posture; None when the read dropped."""
    report = _try_role(ctx, base)
    if not isinstance(report, dict):
        return None
    return {'role': report.get('role'), 'tick': report.get('tick'),
            'tracking': 'tracking' in (report.get('sync') or {})}


def _foreign_view(ctx, seat):
    """One monitor read of a born seat — role, observed claim posture,
    and scan tick, the verdict watch's evidence tuple."""
    base = ctx.get(seat)
    if not base:
        return None
    report = _try_role(ctx, base)
    if not isinstance(report, dict):
        return None
    return {'role': report.get('role'),
            'field_claim': report.get('field_claim'),
            'tick': report.get('tick')}


def _foreign_state(ctx, seat):
    """The seat container's process verdict through the runner's
    read-only state lever — the refused launch's exit evidence."""
    state = ctx.get('born_controller_state')
    if state is None:
        return None
    try:
        return state(seat)
    except Exception:
        return None


def _foreign_launch(ctx, seat, remote, settle=False):
    """Launch one labeled born-active declaring the rig model against
    `remote` and read the launch's disposition: 'refused' — the process
    exited (the named startup refusal); 'active' — a role read showed
    promoting/active or a held claim — the claim landed; 'pending' —
    a stable standby surface that never claimed; 'unread' — no verdict
    arrived inside the bound. `named` reports the correspondence
    verdict's wording on the run's log tail. `settle` keeps polling a
    standby surface until the deadline — the control launch's claim
    must land, not stand — while the mismatched launch's watch breaks
    once the pending surface has stood FOREIGN_WATCH reads."""
    try:
        ctx['start_born_controller'](seat, remote)
    except Exception as exc:
        return {'seat': seat, 'stage_error': str(exc)[:300]}
    views = []
    state = None
    deadline = time.monotonic() + FOREIGN_SETTLE
    while time.monotonic() < deadline:
        seen = _foreign_state(ctx, seat)
        if seen is not None:
            state = seen
            if state.get('exit') is not None \
                    and not state.get('running'):
                break
        view = _foreign_view(ctx, seat)
        if view is not None:
            views.append(view)
            if view.get('role') in ('active', 'promoting') \
                    or view.get('field_claim') == 'held':
                break
            if not settle and len(views) >= FOREIGN_WATCH:
                break
        time.sleep(FOREIGN_POLL)
    if state is None:
        state = _foreign_state(ctx, seat)
    state = state or {}
    logs = str(state.get('logs') or '')
    landed = [view for view in views
              if view.get('role') in ('active', 'promoting')
              or view.get('field_claim') == 'held']
    if landed:
        disposition = 'active'
    elif not state.get('running') \
            and isinstance(state.get('exit'), int) \
            and not isinstance(state.get('exit'), bool):
        disposition = 'refused'
    elif views and all(view.get('role') == 'standby'
                       and view.get('field_claim') != 'held'
                       for view in views):
        disposition = 'pending'
    else:
        disposition = 'unread'
    return {'seat': seat, 'remote': remote, 'disposition': disposition,
            'named': any(marker in logs for marker in MISMATCH_MARKERS),
            'running': state.get('running'), 'exit': state.get('exit'),
            'views': views[-FOREIGN_WATCH:],
            'logs_tail': logs[-400:]}


def _foreign_driven(foreign):
    """Whether the field's own tick advanced across the mismatched
    launch — a refused or pending run must never step the foreign
    plant. None when either ping went unanswered."""
    before, after = foreign.get('tick_before'), foreign.get('tick_after')
    if not isinstance(before, int) or isinstance(before, bool) \
            or not isinstance(after, int) \
            or isinstance(after, bool):
        return None
    return after > before


def _foreign_pair_held(record):
    """The deployed pair's undisturbed verdict: the owner still active
    and advancing its scan across the leg's scratch launches, the peer
    still a tracking standby — before and after alike."""
    launch = record.get('launch') or {}
    roles = record.get('roles') or {}
    owner, peer = launch.get('owner'), launch.get('peer')
    before = roles.get('before') or {}
    after = roles.get('after') or {}
    for view in (before, after):
        if (view.get(owner) or {}).get('role') != 'active':
            return False
        seen = view.get(peer) or {}
        if seen.get('role') != 'standby' \
                or seen.get('tracking') is not True:
            return False
    tick0 = (before.get(owner) or {}).get('tick')
    tick1 = (after.get(owner) or {}).get('tick')
    return isinstance(tick0, int) and not isinstance(tick0, bool) \
        and isinstance(tick1, int) and not isinstance(tick1, bool) \
        and tick1 > tick0


def _foreign_proven(foreign):
    """The staged field is provably foreign to the rig model: the rig's
    declared absent point answers unserved, and the shared kind point
    answers a kind the rig does not declare."""
    kind = foreign.get('kind_point')
    return foreign.get('absent_point') == 'unserved' \
        and isinstance(kind, str) \
        and kind not in ('unserved', 'unread', 'sampled', RIG_KIND)


def _foreign_field_read(ctx, record):
    """The foreign field's census: the served point list, the two
    divergent probes the rig model declares against, and the baseline
    claim/tick reads — ordered so the claim probe's own step precedes
    the tick baseline it would otherwise inflate."""
    foreign = record['foreign']
    foreign['census'] = _foreign_census(ctx)
    foreign['absent_point'] = \
        _foreign_sample(ctx, FOREIGN_ABSENT_POINT)
    foreign['kind_point'] = _foreign_sample(ctx, FOREIGN_KIND_POINT)
    foreign['claim_before'] = _foreign_claim(ctx)
    foreign['tick_before'] = _foreign_ping(ctx)


def _foreign_control(ctx, record):
    """The positive control — the same labeled born-active launch
    shape against a same-model scratch field: the claim must land and
    the run must scan while the field's tick advances under it."""
    control = record['control']
    try:
        field = ctx['start_born_field']('serving')
    except Exception as exc:
        control['stage_error'] = str(exc)[:300]
        return
    control['remote'] = field.get('remote')
    control['census'] = _foreign_census(ctx)
    control['claim_before'] = _foreign_claim(ctx)
    control['tick_before'] = _foreign_ping(ctx)
    launched = _foreign_launch(ctx, CONTROL_SEAT,
                               field.get('remote'), settle=True)
    control['launch'] = launched
    watch = []
    for _ in range(FOREIGN_WATCH):
        view = _foreign_view(ctx, CONTROL_SEAT)
        if view is not None:
            watch.append(view)
        time.sleep(FOREIGN_POLL)
    control['watch'] = watch
    control['field_ticks'] = [control.get('tick_before'),
                              _foreign_ping(ctx)]
    control['claim_after'] = _foreign_claim(ctx)


def _judge_foreign(record, note):
    """Replay one pass's record — runnable against planted negatives in
    the self-check. `note(key, diagnostic, detail)` records each clause
    the record violates: FOREIGN_FAILED tags the contract clauses and
    FOREIGN_NONDET the instability the contract does not answer for."""
    def failed(key, detail):
        note(key, FOREIGN_FAILED, detail)

    def nondet(key, detail):
        note(key, FOREIGN_NONDET, detail)

    if record.get('stage_error') is not None:
        nondet('stage', 'the foreign-field staging never completed: '
               + str(record['stage_error']))
        return
    foreign = record.get('foreign') or {}
    verdict = record.get('mismatched') or {}
    control = record.get('control') or {}

    if verdict.get('stage_error') is not None:
        nondet('mismatched-stage', 'the mismatched launch never '
               'staged: ' + str(verdict['stage_error']))
        return
    disposition = verdict.get('disposition')
    if disposition == 'active':
        failed('mismatched-claimed', 'the mismatched --remote launch '
               'claimed the foreign field and went active — the named '
               'refusal must land before the claim: '
               + json.dumps(verdict.get('views'))[:300])
    elif disposition == 'refused':
        if verdict.get('named') is not True:
            if _foreign_proven(foreign):
                failed('mismatched-unnamed', 'the refused launch '
                       'exited without naming the correspondence '
                       'verdict — on a field proven foreign the '
                       'refusal must say what it refused: '
                       + (verdict.get('logs_tail') or '')[-200:])
            else:
                nondet('mismatched-unnamed', 'the refused launch '
                       'exited without the named verdict and the '
                       'field\'s foreignness is unproven — the exit '
                       'cannot be read as the contract\'s refusal')
    elif disposition == 'pending':
        if verdict.get('named') is not True:
            nondet('mismatched-unverdicted', 'the mismatched launch '
                   'stood pending without the named correspondence '
                   'verdict surfacing — the deferred probe\'s refusal '
                   'never arrived inside the bound: '
                   + json.dumps(verdict.get('views'))[:300])
    else:
        nondet('mismatched-unread', 'the mismatched launch produced '
               'no readable verdict: ' + json.dumps(verdict)[:300])

    if foreign.get('claim_before') == 'claimed':
        nondet('field-claim-before', 'the fresh foreign field '
               'already carried a standing claim')
    claim_after = foreign.get('claim_after')
    if claim_after == 'claimed':
        failed('field-claimed', 'the foreign field carries the '
               'mismatched run\'s claim — the named refusal never '
               'reached the field\'s arbitration')
    elif claim_after != 'unclaimed':
        nondet('field-claim-unread', 'the foreign field never '
               'answered the claim probe — the leg cannot prove the '
               'claim never landed')
    if foreign.get('driven') is True:
        failed('field-driven', 'the foreign field\'s tick advanced '
               'across the mismatched launch — the refused run drove '
               'the plant it must never write')
    elif foreign.get('driven') is None:
        nondet('field-tick-unread', 'the foreign field\'s tick never '
               'answered — the leg cannot prove the run never drove')

    absent = foreign.get('absent_point')
    kind = foreign.get('kind_point')
    if foreign.get('census') is None or absent is None or kind is None:
        nondet('foreignness-unread', 'the foreign field never '
               'answered its census — the served-model divergence '
               'is unproven')
    elif absent != 'unserved' \
            or kind in ('unserved', 'unread', 'sampled', RIG_KIND):
        nondet('foreignness-contradicted', 'the foreign field\'s '
               'census does not corroborate the named mismatch — '
               'point ' + str(FOREIGN_ABSENT_POINT) + ' answered '
               + str(absent) + ', point ' + str(FOREIGN_KIND_POINT)
               + ' served as ' + str(kind))

    launch = control.get('launch') or {}
    if control.get('stage_error') is not None or not launch:
        nondet('control-stage', 'the same-model control staging '
               'never completed: ' + str(control.get('stage_error')))
    elif launch.get('stage_error') is not None:
        nondet('control-launch', 'the same-model control launch '
               'never ran: ' + str(launch['stage_error']))
    else:
        disposition = launch.get('disposition')
        if disposition == 'refused' and launch.get('named'):
            failed('control-refused', 'the same-model --remote '
                   'launch met the named correspondence refusal — '
                   'the probe convicts a matching model: '
                   + (launch.get('logs_tail') or '')[-200:])
        elif disposition != 'active':
            nondet('control-launch', 'the same-model control launch '
                   'never claimed the field — disposition '
                   + str(disposition))
        else:
            watch = control.get('watch') or []
            if not watch:
                nondet('control-watch', 'the control\'s monitor '
                       'never answered the watch reads')
            else:
                if not all(view.get('role') == 'active'
                           and view.get('field_claim') == 'held'
                           for view in watch):
                    failed('control-dropped', 'the control\'s '
                           'active+held surface did not hold across '
                           'the watch: ' + json.dumps(watch)[:300])
                ticks = [view.get('tick') for view in watch]
                if not all(isinstance(tick, int)
                           and not isinstance(tick, bool)
                           for tick in ticks) \
                        or ticks[0] >= ticks[-1]:
                    failed('control-scan-stalled', 'the control '
                           'claims the field but its scan tick does '
                           'not advance: ' + json.dumps(watch)[:300])
            ticks = control.get('field_ticks') or []
            if len(ticks) != 2 or not all(
                    isinstance(tick, int) and not isinstance(tick, bool)
                    for tick in ticks):
                nondet('control-field-unread', 'the control field\'s '
                       'tick never answered')
            elif ticks[1] <= ticks[0]:
                failed('control-field-stalled', 'the control claimed '
                       'but the field\'s tick never advanced — it '
                       'owns without scanning')
            claim_after = control.get('claim_after')
            if claim_after == 'unclaimed':
                failed('control-unclaimed', 'the control reports a '
                       'held claim but the field answers unclaimed — '
                       'the claim never landed')
            elif claim_after != 'claimed':
                nondet('control-claim-unread', 'the control field '
                       'never answered the claim probe')
            if control.get('claim_before') == 'claimed':
                nondet('control-claim-before', 'the fresh same-model '
                       'field already carried a standing claim')

    if not _foreign_pair_held(record):
        nondet('pair-disturbed', 'the deployed pair moved or wedged '
               'across the scratch launches: '
               + json.dumps(record.get('roles'), sort_keys=True)[:300])


def _foreign_digest(record, violations):
    """The pass's normalized verdict set — identical digests across
    two consecutive passes is the determinism contract."""
    def clean(*keys):
        return not any(key in violations for key in keys)
    verdict = record.get('mismatched') or {}
    if verdict.get('named') is not True:
        mismatched = 'defect'
    elif verdict.get('disposition') == 'refused':
        mismatched = 'refused'
    elif verdict.get('disposition') == 'pending':
        mismatched = 'pending'
    else:
        mismatched = 'defect'
    return {
        'mismatched': mismatched,
        'field': 'untouched' if clean('mismatched-claimed',
                                      'field-claimed', 'field-driven',
                                      'field-claim-unread',
                                      'field-tick-unread',
                                      'field-claim-before')
                 else 'defect',
        'foreignness': 'proved' if clean('foreignness-unread',
                                         'foreignness-contradicted')
                       else 'defect',
        'control': 'claims-and-scans'
                   if clean('control-stage', 'control-launch',
                            'control-refused', 'control-dropped',
                            'control-watch', 'control-scan-stalled',
                            'control-field-stalled',
                            'control-field-unread',
                            'control-unclaimed', 'control-claim-unread',
                            'control-claim-before')
                   else 'defect',
        'pair': 'held' if clean('pair-disturbed') else 'disturbed'}


def _foreign_pass(ctx, number, launch):
    """One pass over the miswire replay: frame the deployed pair's
    roles, stage the foreign field and gather its census, launch the
    mismatched --remote born-active and read its verdict, probe the
    field's claim and tick, then — once the named verdict is present —
    run the same-model control, and frame the pair again."""
    record = {'pass': number, 'launch': dict(launch), 'roles': {},
              'foreign': {}, 'mismatched': {}, 'control': {}}
    owner, peer = launch['owner'], launch['peer']
    record['roles']['before'] = {
        name: _foreign_role(ctx, ctx[name]) for name in (owner, peer)}
    try:
        field = ctx['start_born_field']('foreign')
    except Exception as exc:
        record['stage_error'] = str(exc)[:300]
    else:
        record['remote'] = field.get('remote')
        _foreign_field_read(ctx, record)
        record['mismatched'] = _foreign_launch(ctx, MISMATCH_SEAT,
                                               field.get('remote'))
        foreign = record['foreign']
        foreign['tick_after'] = _foreign_ping(ctx)
        foreign['driven'] = _foreign_driven(foreign)
        foreign['claim_after'] = _foreign_claim(ctx)
        try:
            ctx['stop_born_controller'](MISMATCH_SEAT)
        except Exception:
            pass
        verdict = record['mismatched']
        if verdict.get('stage_error') is not None:
            pass  # the judge reports the failed launch staging
        elif verdict.get('named') is not True:
            # The inconclusive gate is narrow: only a launch whose
            # claim LANDED on a proven-foreign field names the
            # pre-contract shape — the probe absent, the born-active
            # owning the foreign plant — and only an unproven census
            # leaves that same landing unattributable. Every other
            # unnamed disposition is the judge's to classify: an
            # unnamed refusal is the named-diagnostic clause's own
            # failure, an unread or unverdicted surface the
            # nondeterministic class's.
            landed = verdict.get('disposition') == 'active' \
                or foreign.get('claim_after') == 'claimed' \
                or foreign.get('driven') is True
            if landed and _foreign_proven(foreign):
                record['inconclusive'] = (
                    'the staged revision predates the '
                    'remote-attachment correspondence contract — the '
                    '--remote born-active claimed and went active on '
                    'the foreign field')
            elif landed:
                record['inconclusive'] = (
                    'the staged field is not foreign or never '
                    'answered its census — the revision cannot be '
                    'read against the contract')
        else:
            _foreign_control(ctx, record)
    record['roles']['after'] = {
        name: _foreign_role(ctx, ctx[name]) for name in (owner, peer)}
    return record


def _foreign_self_check():
    """The unchecked-diagnostic guard: replay the judge over planted
    negatives — a refusal asserted while the mismatched run claimed
    and went active, a foreign field still claimed or still stepping,
    a census that contradicts the named mismatch, the control refused
    by name or claiming without scanning, a moved pair — and report
    every one let slip."""
    refusal = 'error: plant server at dcs-hw-qa-1-born-plant:9003 ' \
        'does not serve io point 120: unknown I/O point PointId(120)'

    def clean_record():
        return {'pass': 1,
                'launch': {'owner': 'active', 'peer': 'standby'},
                'remote': 'dcs-hw-qa-1-born-plant:9003',
                'foreign': {'census': [10, 11, 12, 20, 40, 100],
                            'absent_point': 'unserved',
                            'kind_point': 'bool',
                            'claim_before': 'unclaimed',
                            'tick_before': 3, 'tick_after': 3,
                            'driven': False, 'claim_after': 'unclaimed'},
                'mismatched': {'seat': MISMATCH_SEAT,
                               'disposition': 'refused', 'named': True,
                               'running': False, 'exit': 1,
                               'views': [], 'logs_tail': refusal},
                'control': {'launch': {'seat': CONTROL_SEAT,
                                       'disposition': 'active',
                                       'named': False,
                                       'running': True, 'exit': None,
                                       'views': [], 'logs_tail': ''},
                            'census': [10, 11, 12, 20, 40, 100, 120],
                            'claim_before': 'unclaimed',
                            'tick_before': 0,
                            'watch': [{'role': 'active',
                                       'field_claim': 'held',
                                       'tick': 41},
                                      {'role': 'active',
                                       'field_claim': 'held',
                                       'tick': 42}],
                            'field_ticks': [0, 4],
                            'claim_after': 'claimed'},
                'roles': {
                    'before': {
                        'active': {'role': 'active', 'tick': 10,
                                   'tracking': False},
                        'standby': {'role': 'standby', 'tick': 10,
                                    'tracking': True}},
                    'after': {
                        'active': {'role': 'active', 'tick': 12,
                                   'tracking': False},
                        'standby': {'role': 'standby', 'tick': 12,
                                    'tracking': True}}}}

    def audit(record):
        found = {}
        _judge_foreign(
            record, lambda key, diagnostic, detail:
            found.setdefault(key, diagnostic))
        return found

    slipped = []
    if audit(clean_record()):
        slipped.append('clean-overstrict')

    def expect(name, mutate, diagnostic=FOREIGN_FAILED):
        record = clean_record()
        mutate(record)
        if diagnostic not in audit(record).values():
            slipped.append(name)

    # The doctored negative the issue names — the refusal asserted
    # while the mismatched controller claims and goes active.
    expect('refusal-asserted-but-active', lambda r:
           r['mismatched'].update(
               disposition='active', running=True, exit=None,
               views=[{'role': 'active', 'field_claim': 'held',
                       'tick': 5}]))
    expect('refusal-asserted-but-claimed', lambda r:
           r['foreign'].update(claim_after='claimed'))
    expect('refusal-asserted-but-driven', lambda r:
           r['foreign'].update(tick_after=9, driven=True))
    expect('mismatched-verdict-unnamed', lambda r:
           r['mismatched'].update(named=False))
    expect('control-met-the-named-refusal', lambda r:
           r['control']['launch'].update(
               disposition='refused', named=True,
               exit=1, logs_tail=refusal))
    expect('control-reported-held-unclaimed', lambda r:
           r['control'].update(claim_after='unclaimed'))
    expect('control-claims-without-scanning', lambda r:
           r['control'].update(field_ticks=[4, 4]))
    expect('control-drops-the-claim', lambda r:
           r['control']['watch'][1].update(
               role='standby', field_claim=None))
    expect('control-scan-tick-stalls', lambda r:
           r['control']['watch'][1].update(
               tick=r['control']['watch'][0]['tick']))
    expect('mismatched-verdict-unread', lambda r:
           r['mismatched'].update(disposition='unread', named=False),
           FOREIGN_NONDET)
    expect('mismatched-pending-unverdicted', lambda r:
           r['mismatched'].update(disposition='pending', named=False),
           FOREIGN_NONDET)
    expect('mismatched-unnamed-unproven', lambda r:
           (r['mismatched'].update(named=False),
            r['foreign'].update(kind_point='float')),
           FOREIGN_NONDET)
    expect('field-claim-probe-unread', lambda r:
           r['foreign'].update(claim_after='unread'),
           FOREIGN_NONDET)
    expect('field-tick-probe-unread', lambda r:
           r['foreign'].update(tick_after=None, driven=None),
           FOREIGN_NONDET)
    expect('foreign-census-unread', lambda r:
           r['foreign'].update(census=None),
           FOREIGN_NONDET)
    expect('foreign-census-contradicts', lambda r:
           r['foreign'].update(kind_point='float'),
           FOREIGN_NONDET)
    expect('foreign-field-preclaimed', lambda r:
           r['foreign'].update(claim_before='claimed'),
           FOREIGN_NONDET)
    expect('control-never-claimed', lambda r:
           r['control']['launch'].update(disposition='pending'),
           FOREIGN_NONDET)
    expect('control-stage-refused', lambda r:
           r['control'].update(stage_error='docker run failed'),
           FOREIGN_NONDET)
    expect('pair-owner-moved', lambda r:
           r['roles']['after']['active'].update(role='standby'),
           FOREIGN_NONDET)
    expect('pair-peer-lost-tracking', lambda r:
           r['roles']['after']['standby'].update(tracking=False),
           FOREIGN_NONDET)
    expect('pair-scan-wedged', lambda r:
           r['roles']['after']['active'].update(tick=10),
           FOREIGN_NONDET)
    expect('staging-refused', lambda r:
           r.update(stage_error='docker run failed'),
           FOREIGN_NONDET)
    return slipped


def _foreign_teardown(ctx):
    """Best-effort teardown: the mismatched and control seats plus the
    scratch field — a clean pass leaves nothing standing, and an
    aborted pass gets the same sweep so the legs behind this one see
    free seats and a free name."""
    lever = ctx.get('stop_born_controller')
    if lever is not None:
        for seat in (MISMATCH_SEAT, CONTROL_SEAT):
            try:
                lever(seat)
            except Exception:
                pass
    try:
        if ctx.get('stop_born_field') is not None:
            ctx['stop_born_field']()
    except Exception:
        pass


def scenario_remote_foreign_model(ctx):
    """Exercise the remote-attachment foreign-model correspondence
    refusal: a labeled born-active declaring the rig model is launched
    --remote at a scratch field serving a different model, and the
    startup claim must never land — the named correspondence verdict
    refuses it, the field's arbitration stays unclaimed, its tick
    unmoved — while the same launch shape against a same-model field
    claims and scans, and the deployed pair's run is undisturbed
    throughout. Two consecutive passes must produce identical
    digests."""
    case = Case(
        'remote-foreign-model',
        'A --remote born-active never claims a foreign-model field',
        'a labeled scratch field serving a model whose point set and '
        'kinds differ from the rig\'s meets the rig-model '
        'born-active\'s startup claim with the named correspondence '
        'refusal — never an active run owning a foreign plant — while '
        'a same-model remote launch of the same shape claims and '
        'scans normally, the deployed pair\'s roles and scan never '
        'move, and two passes produce identical digests')
    try:
        missing = [key for key in ('start_born_field',
                                   'stop_born_field',
                                   'start_born_controller',
                                   'stop_born_controller',
                                   'born_controller_state',
                                   'born_field_ctl')
                   if ctx.get(key) is None]
        if missing:
            return case.finish('inconclusive',
                               'the run context carries no '
                               'born-active staging levers: '
                               + ', '.join(missing))
        if not all(ctx.get(seat)
                   for seat in (MISMATCH_SEAT, CONTROL_SEAT)):
            return case.finish('inconclusive', 'the run context '
                               'carries no published monitor for '
                               'the leg\'s born seats')
        deadline = time.monotonic() + FOREIGN_SETTLE
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=FOREIGN_POLL)
        if owner is None:
            reports = {name: _try_role(ctx, ctx[name])
                       for name in ('active', 'standby')
                       if ctx.get(name)}
            if not reports or all(report is None
                                  for report in reports.values()):
                return case.finish(
                    'inconclusive', 'the deployed pair is '
                    'unreachable — monitor endpoints '
                    + str(ctx.get('active')) + ' and '
                    + str(ctx.get('standby')))
            return case.finish('failed', 'no peer reports '
                               'role=active')
        peer = 'standby' if owner == 'active' else 'active'
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=FOREIGN_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the settled '
                               'posture the leg proves undisturbed '
                               'was never reached')
        launch = {'owner': owner, 'peer': peer}
        case.observe('field owner: ' + owner + ' (' + ctx[owner]
                     + '); tracking peer: ' + peer + ' ('
                     + ctx[peer] + ')')
        digests = []
        for number in (1, 2):
            violations = {}

            def note(key, diagnostic, detail):
                violations.setdefault(key, (diagnostic, detail))

            try:
                record = _foreign_pass(ctx, number, launch)
            finally:
                # Each pass ends with the rig swept — the seats and
                # the scratch field removed so the next pass and the
                # legs behind this one start cold, and the launch
                # configuration is restored as found.
                _foreign_teardown(ctx)
            if not record.get('inconclusive'):
                _judge_foreign(record, note)
            digest = _foreign_digest(record, violations)
            record['digest'] = dict(digest)
            record['violations'] = {
                key: diagnostic
                for key, (diagnostic, _) in violations.items()}
            ref = save_evidence(
                ctx['evidence_dir'],
                'remote-foreign-model-pass-' + str(number) + '.json',
                record)
            case.evidence('file', ref,
                          'remote-foreign-model pass '
                          + str(number) + ' — the foreign field\'s '
                          'census and claim probes, the mismatched '
                          'launch\'s verdict, the control\'s claim '
                          'and scans, the pair framing, and the '
                          'normalized digest')
            if record.get('inconclusive'):
                return case.finish('inconclusive',
                                   record['inconclusive'])
            if violations:
                name = FOREIGN_FAILED if any(
                    diagnostic == FOREIGN_FAILED
                    for diagnostic, _ in violations.values()) \
                    else FOREIGN_NONDET
                return case.finish(
                    'failed', name + ': ' + '; '.join(
                        detail for _, detail in
                        list(violations.values())[:4]))
            digests.append(digest)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', FOREIGN_NONDET + ': the two passes\' '
                'digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two foreign-model passes, identical digests: '
                     + json.dumps(digests[0], sort_keys=True))
        slipped = _foreign_self_check()
        if slipped:
            return case.finish('failed', FOREIGN_UNCHECKED
                               + ': planted negatives slipped the '
                               'leg’s own audits: '
                               + ', '.join(slipped))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
