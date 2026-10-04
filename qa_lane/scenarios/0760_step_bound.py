"""The step-bound acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: The step-bound leg shares the restored pre-switch window
# the field-claim case re-establishes and the nonfinite-refusal case
# leaves undisturbed — the settled pair's pinned owner token is the
# standing claim its shared attachment joins, the refused steps move no
# role and no field state, and the pair must still sit on its launch
# roles for the fenced-writer case's misordered promote.
RUNS_AFTER = frozenset({'scenario_nonfinite_refusal'})
RUNS_BEFORE = frozenset({'scenario_fenced_writer_degrade'})


# --------------------------------------------------------------------
# The bounded plant-step contract (the #683 fix's per-revision rig
# evidence — WW-OPS-003's field-confidence clause and protocol.rs's
# refuse-rather-than-panic rule): a step's `dt` is one scan period, and
# the field bounds it at MAX_STEP_DT, so a request carrying a *finite*
# but huge `dt` is refused by name instead of applied. The finding's
# reproduction — `claim_writer(X); step(1e308); read(...)` — is
# protocol-legal JSON: `1e308` is a finite f64, so nothing at the
# request boundary could refuse it as unspellable. Applied, it wound
# the element's accumulator past the f64 range or parked an accumulated
# clock where no legal later step could move it again; either way reads
# and list_points then served `{"float":null}`, which this protocol's
# own `Value` cannot deserialize, and a write plus a step could not
# repair it because the element recomputed from the corrupted state.
#
# Two probes, in this order deliberately. The ordinary over-bound
# advance runs FIRST so that a rig predating the bound absorbs a step
# the field can survive and never sees the finding's `1e308` vector
# applied; a rig whose first probe is applied reports inconclusive
# rather than judging a contract it cannot see. On a rig carrying the
# contract both are refused with the bound named and change nothing.
#
# The seam split follows the nonfinite-refusal leg: the census and the
# reads ride the shipped `dcs-plant-ctl` — the tool's own decode IS
# the deserialization proof, a `{"float":null}` answer being exactly
# what the shipped binary cannot print — while the claim ensure and the
# over-bound steps stay on the raw client, the tool refusing a huge dt
# at its own argument boundary instead of putting it on the wire. The
# probe vector itself needs no raw-line seam: `1e308` is a finite
# number, which is the whole of the finding's difficulty.
#
# What pacing cannot move is what this leg measures: the lane's field is
# paced by its standing owner, so a served sample legitimately moves
# between two reads and only an element-undriven point's stored value
# can be compared across the probes — the census's whole-image
# equality is the consumer leg's assertion, over a pair nothing steps
# but the harness. What the lane adds is the poisoned-frame scan over
# every served float, the undriven point's stored value, the step
# path's continued liveness, and the pair's own posture. Contract
# violations name step-bound-failed; answers that disagree with the
# field state they report name step-bound-nondeterministic.
#
# The refused-probe clauses are pure predicates over one observation
# record (`_step_bound_violation`), and `_step_bound_self_check`
# replays them over every planted negative the contract names before
# the leg trusts itself on the rig — a self-check that stayed silent on,
# or wrongly named, one reports step-bound-unchecked rather than
# passing.

STEP_BOUND_DEADLINE = 30   # bound on each settle/growth watch
STEP_BOUND = 1.0e6         # dcs_sim::MAX_STEP_DT — the documented
                           # per-tick bound, in time units
OVER_BOUND_DT = 1.0e7      # an ordinary advance past the bound: the
                           # probe a pre-bound rig absorbs
HUGE_DT = 1.0e308          # the finding's own reproduction vector
STEP_BOUND_FOLLOW = 0.25   # the follow-up step's finite advance
BOUND_DETAIL = '1000000'   # the bound as the refusal's detail spells
                           # it — `format!("{}", 1.0e6)` on the field


def _float_payload(value):
    """(is_float, raw) — the float leg of a served Value dict and its
    payload as served: the number, or None/NaN/inf on the poisoned
    `{"float":null}`-family frame the contract forbids. (False, None)
    for the other value kinds."""
    if isinstance(value, dict) and 'float' in value:
        return True, value.get('float')
    return False, None


def _finite_float(value):
    """A served Value dict's float payload when it is a finite
    number — the only Float the wire contract can spell — else
    None."""
    is_float, raw = _float_payload(value)
    if is_float and isinstance(raw, (int, float)) \
            and not isinstance(raw, bool) and math.isfinite(raw):
        return raw
    return None


def _poisoned_entry(entries):
    """The first point entry whose served sample carries a float
    payload that is not a finite number — the `{"float":null}`
    poisoning shape the finding records — or None while every served
    float decodes finite."""
    for entry in entries:
        value = (entry.get('sample') or {}).get('value')
        is_float, raw = _float_payload(value)
        if is_float and (not isinstance(raw, (int, float))
                         or isinstance(raw, bool)
                         or not math.isfinite(raw)):
            return entry
    return None


def _claim_verdict(response):
    """A step answer's claim-state refusal — 'fenced' or 'unclaimed',
    the io-carried fenced verdict included — or None: the answer that
    means the probe met the claim contract rather than the bound under
    test."""
    error = (response or {}).get('error')
    if not isinstance(error, dict):
        return None
    if error.get('kind') in ('fenced', 'unclaimed'):
        return error['kind']
    inner = error.get('error')
    if error.get('kind') == 'io' and isinstance(inner, dict) \
            and 'fenced' in inner:
        return 'fenced'
    return None


def _bound_refusal(response):
    """The named refusal an over-bound step's answer carries:
    'bound' when the request was refused as `invalid_request` with the
    documented bound in its detail, 'invalid_request' when it was
    refused without that detail, or None on any other answer — a
    `stepped` verdict included, which is how a rig predating the bound
    is recognized."""
    error = (response or {}).get('error')
    if not isinstance(error, dict) \
            or error.get('kind') != 'invalid_request':
        return None
    detail = error.get('detail')
    if isinstance(detail, str) and BOUND_DETAIL in detail:
        return 'bound'
    return 'invalid_request'


def _ctl_decodes(response, want):
    """Whether a `dcs-plant-ctl` answer is the named result — the
    shipped client's own decode succeeding. A refused invocation comes
    back error-shaped; a poisoned `{"float":null}` frame is the decode
    failure its nonzero exit carries."""
    return isinstance(response, dict) and response.get('result') == want


def _served_active(roles):
    """The peer whose served role report names it active, or None — the
    pure form of `_settled_active` over reports already gathered, so the
    audit reaches the pair's posture without polling again."""
    for name in sorted(roles or {}):
        report = roles[name]
        if isinstance(report, dict) and report.get('role') == 'active':
            return name
    return None


# The record keys the audit reads, per phase: what must be gathered
# before the phase's clauses can hold.
_PHASE_KEYS = {
    'refusals': ('point', 'baseline', 'undriven', 'answers', 'canary',
                 'read', 'census'),
    'followup': ('point', 'baseline', 'undriven', 'canary', 'follow',
                 'landed', 'census2'),
    'restored': ('point', 'baseline', 'undriven', 'restored', 'final'),
    'pair': ('point', 'baseline', 'undriven', 'active', 'tracking',
             'roles1', 'snaps0', 'grown', 'image'),
}


def _step_bound_violation(record, phase):
    """The refused-probe contract's clauses as pure predicates over the
    observations the leg has gathered by `phase` — the phase decides
    which clauses are live, so the audit never judges state it has not
    seen while the leg still stops at the first clause it catches.
    Returns `(key, diagnostic, detail)` for the first clause `record`
    violates in contract order, or None while the record holds it.

    `record` carries what each phase needs: the driven `point`, its
    `baseline` Value dict, whether it is `undriven` (element-free, so
    its stored value is comparable across the probes), the `answers` the
    probes got, the `canary` `dt:0` step, the shipped client's answers
    (`read`/`census` after the refusals, `landed`/`census2` after the
    finite step, `restored`/`final` after the restore), and the pair's
    own posture (`active`, `tracking`, `roles1`, `snaps0`, `grown`,
    `image`). A missing key means the phase has not reached it, which is
    why the live clauses guard on the value being there at all."""
    for key in _PHASE_KEYS.get(phase, ()):
        if record.get(key) is None:
            return None
    point = record.get('point')
    baseline = record.get('baseline')
    undriven = record.get('undriven')

    if phase == 'refusals':
        read_back = record.get('read')
        census = record.get('census')
        if not _ctl_decodes(read_back, 'sample'):
            return ('read-undecodable', 'step-bound-failed',
                    'the refused steps left state the shipped client '
                    'cannot deserialize — read on point ' + str(point)
                    + ' answered ' + json.dumps(read_back)[:300])
        served = (read_back.get('sample') or {}).get('value')
        if _float_payload(served)[0] and _finite_float(served) is None:
            return ('read-non-finite', 'step-bound-failed',
                    'the driven point serves a non-finite float — the '
                    '{"float":null} poisoning the bound exists to '
                    'prevent: ' + json.dumps(served)[:300])
        if undriven and served != baseline:
            return ('read-moved', 'step-bound-nondeterministic',
                    'the steps answered ' + json.dumps(record.get(
                        'answers')) + ' yet the driven point\'s stored '
                    'value moved — ' + json.dumps(baseline) + ' -> '
                    + json.dumps(served)[:300])
        if not _ctl_decodes(census, 'points'):
            return ('census-undecodable', 'step-bound-failed',
                    'the post-refusal census no longer deserializes for '
                    'the shipped client: ' + json.dumps(census)[:300])
        poisoned = _poisoned_entry((census or {}).get('points') or [])
        if poisoned is not None:
            return ('census-poisoned', 'step-bound-failed',
                    'point ' + str(poisoned.get('point')) + ' serves a '
                    'non-finite sample the wire cannot spell — an '
                    'over-bound step still poisoned the field: '
                    + json.dumps(poisoned.get('sample'))[:300])

    elif phase == 'followup':
        follow = record.get('follow')
        landed = record.get('landed')
        census2 = record.get('census2')
        if follow.get('result') != 'stepped':
            return ('follow-refused', 'step-bound-failed',
                    'a finite step under the same claim was refused '
                    'after the bound refusals — the probes wedged the '
                    'step path: ' + json.dumps(follow)[:300])
        if not isinstance(follow.get('tick'), int) \
                or follow['tick'] <= ((record.get('canary') or {}).get(
                    'tick') or 0):
            return ('follow-static', 'step-bound-nondeterministic',
                    'the finite step answered stepped but the plant tick '
                    'never advanced past the pre-probe canary — '
                    + json.dumps((record.get('canary') or {}).get('tick'))
                    + ' -> ' + json.dumps(follow.get('tick')))
        if not _ctl_decodes(landed, 'sample'):
            return ('landed-undecodable', 'step-bound-failed',
                    'the post-step read no longer deserializes for the '
                    'shipped client: ' + json.dumps(landed)[:300])
        poisoned = _poisoned_entry((census2 or {}).get('points') or [])
        if not _ctl_decodes(census2, 'points') or poisoned is not None:
            return ('follow-census', 'step-bound-failed',
                    'the finite step left a census the shipped client '
                    'cannot read finite — ' + json.dumps(census2)[:300])

    elif phase == 'restored':
        restored = record.get('restored')
        final = record.get('final')
        if restored.get('result') != 'done':
            return ('restore-refused', 'step-bound-failed',
                    'the restore write was refused — the field did not '
                    'come back: ' + json.dumps(restored)[:300])
        if not _ctl_decodes(final, 'sample'):
            return ('restored-undecodable', 'step-bound-failed',
                    'the restored point no longer deserializes for the '
                    'shipped client: ' + json.dumps(final)[:300])
        served = (final.get('sample') or {}).get('value')
        if undriven and served != baseline:
            return ('restore-moved', 'step-bound-nondeterministic',
                    'the restore write answered done but the point '
                    'reads ' + json.dumps(served)[:300] + ', not the '
                    'baseline ' + json.dumps(baseline))

    elif phase == 'pair':
        roles1 = record.get('roles1') or {}
        active = record.get('active')
        tracking = record.get('tracking')
        if (roles1.get(active) or {}).get('role') != 'active' \
                or _served_active(roles1) != active:
            return ('active-moved', 'step-bound-failed',
                    'the refused steps moved the active role: '
                    + json.dumps(roles1)[:300])
        if (roles1.get(tracking) or {}).get('role') != 'standby':
            return ('peer-moved', 'step-bound-failed',
                    'the refused steps moved the tracking peer\'s role: '
                    + json.dumps(roles1)[:300])
        snaps0 = record.get('snaps0') or {}
        grown = record.get('grown') or {}
        for name in ('active', 'standby'):
            if grown.get(name) is None:
                return ('scan-stalled-' + name, 'step-bound-failed',
                        name + '\'s scans stalled under the refused '
                        'steps — tick ' + json.dumps((snaps0.get(name)
                                                     or {}).get('tick'))
                        + ' never advanced')
        health0 = (snaps0.get(active) or {}).get('io_health') or {}
        health1 = (record.get('image') or {}).get('io_health') or {}
        for counter in ('failed_reads', 'failed_writes'):
            if (health1.get(counter) or 0) > (health0.get(counter) or 0):
                return ('io-health-' + counter, 'step-bound-failed',
                        'the refused steps counted against the owner\'s '
                        'io_health: ' + json.dumps(health1)[:300])
        if undriven:
            image = _point_value(record.get('image') or {}, point)
            if image != _finite_float(baseline):
                return ('image-moved', 'step-bound-failed',
                        'the field image did not restore — the active '
                        'serves ' + json.dumps(image)[:300]
                        + ' for point ' + str(point) + ', not the '
                        'baseline ' + json.dumps(_finite_float(baseline)))
    return None


def _step_bound_self_check():
    """The leg's unchecked-diagnostic self-test: replay the audit over
    each planted negative the contract names — an off-contract answer
    where the bound refusal stands, an undecodable or poisoned read and
    census, a driven point that moved despite the refusal, a refused
    finite step, a static follow-up tick, a poisoned post-step census, a
    refused restore, a restored point that did not return, a moved role,
    a stalled scan, a counted refusal, and an unrestored field image —
    and require each to trip the diagnostic its class names, while the
    clean record trips nothing. Returns the planted case names the audit
    let through or wrongly named."""
    baseline = {'float': 0.0}

    def record(**over):
        """A held contract up to `phase`, with the phase's clean
        answers filled in."""
        clean = {
            'point': 12,
            'baseline': baseline,
            'undriven': True,
            'answers': {'the over-bound step': 'bound'},
            'canary': {'result': 'stepped', 'tick': 4},
            'read': {'result': 'sample',
                     'sample': {'value': baseline}},
            'census': {'result': 'points', 'points': [
                 {'point': 12, 'sample': {'value': baseline}},
                 {'point': 10, 'sample': {'value': {'float': 0.8}}}]},
            'follow': {'result': 'stepped', 'tick': 5},
            'landed': {'result': 'sample',
                       'sample': {'value': {'float': 0.1}}},
            'census2': {'result': 'points', 'points': [
                {'point': 12, 'sample': {'value': {'float': 0.1}}}]},
            'restored': {'result': 'done'},
            'final': {'result': 'sample', 'sample': {'value': baseline}},
            'active': 'ctrl-a',
            'tracking': 'ctrl-b',
            'roles1': {'ctrl-a': {'role': 'active'},
                       'ctrl-b': {'role': 'standby'}},
            'snaps0': {'ctrl-a': {'tick': 20, 'io_health': {
                          'failed_reads': 0, 'failed_writes': 0}},
                       'ctrl-b': {'tick': 20}},
            'grown': {'ctrl-a': {'tick': 24}, 'ctrl-b': {'tick': 24}},
            'image': {'points': [{'point': 12,
                                  'sample': {'value': baseline}}],
                      'io_health': {'failed_reads': 0, 'failed_writes': 0}},
        }
        clean.update(over)
        return clean

    def moved(point=12):
        return [{'point': point, 'sample': {'value': {'float': 4.75}}}]

    def poisoned(point=12):
        return [{'point': point, 'sample': {'value': {'float': None}}}]

    plants = (
        ('clean', 'refusals', record(), None),
        ('off-contract', 'refusals', record(
            answers={'the over-bound step': 'stepped'}), None),
        ('read-undecodable', 'refusals', record(
            read={'result': 'error'}), 'step-bound-failed'),
        ('read-non-finite', 'refusals', record(
            read={'result': 'sample',
                  'sample': {'value': {'float': None}}}),
         'step-bound-failed'),
        ('read-moved', 'refusals', record(
            read={'result': 'sample',
                  'sample': {'value': {'float': 4.75}}}),
         'step-bound-nondeterministic'),
        ('census-undecodable', 'refusals', record(
            census={'result': 'error'}), 'step-bound-failed'),
        ('census-poisoned', 'refusals', record(
            census={'result': 'points', 'points': poisoned()}),
         'step-bound-failed'),
        ('follow-refused', 'followup', record(
            follow={'result': 'error'}), 'step-bound-failed'),
        ('follow-static', 'followup', record(
            follow={'result': 'stepped', 'tick': 4}),
         'step-bound-nondeterministic'),
        ('landed-undecodable', 'followup', record(
            landed={'result': 'error'}), 'step-bound-failed'),
        ('follow-census', 'followup', record(
            census2={'result': 'points', 'points': poisoned(10)}),
         'step-bound-failed'),
        ('restore-refused', 'restored', record(
            restored={'result': 'error'}), 'step-bound-failed'),
        ('restored-undecodable', 'restored', record(
            final={'result': 'error'}), 'step-bound-failed'),
        ('restore-moved', 'restored', record(
            final={'result': 'sample',
                   'sample': {'value': {'float': 1.25}}}),
         'step-bound-nondeterministic'),
        ('active-moved', 'pair', record(
            roles1={'ctrl-a': {'role': 'standby'},
                    'ctrl-b': {'role': 'standby'}}),
         'step-bound-failed'),
        ('peer-moved', 'pair', record(
            roles1={'ctrl-a': {'role': 'active'},
                    'ctrl-b': {'role': 'active'}}),
         'step-bound-failed'),
        ('scan-stalled', 'pair', record(grown={'ctrl-a': {'tick': 24}}),
         'step-bound-failed'),
        ('io-health', 'pair', record(image={
            'points': [{'point': 12, 'sample': {'value': baseline}}],
            'io_health': {'failed_reads': 3, 'failed_writes': 0}}),
         'step-bound-failed'),
        ('image-moved', 'pair', record(image={
            'points': moved(), 'io_health': {}}), 'step-bound-failed'),
    )
    slipped = []
    for name, phase, held, expect in plants:
        _key, diagnostic, _detail = _step_bound_violation(held, phase) \
            or (None, None, None)
        if diagnostic != expect:
            slipped.append(name)
    return slipped


def scenario_step_bound(ctx):
    """A shared-claim attachment's huge finite step dt meets the
    documented bound by name, leaves every served frame decodable and
    finite with no state change at all, and costs the pair nothing —
    then a finite step lands and the driven point restores."""
    case = Case('step-bound',
                'A huge finite step dt is refused by the documented '
                'bound',
                'with the deployed pair settled and tracking, a '
                'plant-protocol attachment sharing the active\'s '
                'pinned writer claim steps the field with an ordinary '
                'over-bound dt and then with the finding\'s 1e308 '
                'vector: each answers the protocol\'s named refusal '
                'with the documented bound in its detail instead of '
                'applying, the driven undriven point\'s stored value '
                'does not move, subsequent read and list_points '
                'responses keep deserializing finite values for every '
                'driven point — no {"float":null} poisoning survives '
                '— the step path stays live for a finite advance, and '
                'both peers\' scans, served snapshots, and roles are '
                'unaffected, with the driven field point restored')
    stream = None
    restore = None        # (point, baseline Value dict) once known
    try:
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + STEP_BOUND_DEADLINE)
        if active is None:
            reachable = any(
                _try_role(ctx, ctx[name]) is not None
                for name in ('active', 'standby') if ctx.get(name))
            return case.finish(
                'failed' if reachable else 'inconclusive',
                'no peer reports role=active' if reachable
                else 'the pair is unreachable')
        base = ctx[active]
        tracking = wait_for(lambda: _tracking_peer(ctx, active),
                            time.monotonic() + STEP_BOUND_DEADLINE)
        if tracking is None:
            return case.finish('inconclusive', 'no tracking peer — '
                               'the deployed pair never settled')
        if ctx.get('plant') is None:
            return case.finish('inconclusive',
                               'the run publishes no plant endpoint')
        owner = (ctx.get('plant_owner') or {}).get(active)
        if owner is None:
            return case.finish('inconclusive', 'the run pins no '
                               'plant-writer owner token for the '
                               'settled active ' + str(active))
        case.observe('settled pair: ' + active + ' active, '
                     + tracking + ' tracking; probing under the '
                     'shared writer claim')

        stream = _plant_connect(ctx)
        verdict = _plant_request(stream, {'op': 'ensure_writer',
                                          'owner': owner})
        if verdict.get('result') not in ('done', 'claimed_shared'):
            return case.finish('inconclusive', 'the writer claim '
                               'refused the shared attachment under '
                               'the active\'s pinned token — a rig '
                               'predating or mishandling the '
                               'shared-claim contract: '
                               + json.dumps(verdict)[:300])
        case.observe('attached under the standing writer claim ('
                     + str(verdict.get('result')) + ')')

        # The driven point: the model's declared forcing input —
        # 'inflow' in the deployed dynamics, the undriven float the
        # harness legs write — when the census serves it finite; else
        # any field in-point holding a finite float. Only an
        # element-undriven point's stored value can be compared across
        # the probes, so the strict no-state-change check applies to
        # it alone; an element-driven point legitimately moves while
        # the standing owner keeps stepping.
        _, signals = http_json('GET', base + '/signals')
        field = _field_inputs(ctx)
        named = {entry.get('name'): entry.get('point')
                 for entry in (signals or {}).get('points', [])}
        point = named.get('inflow')
        if point not in field \
                or _finite_float(
                    ((field.get(point) or {}).get('sample') or {})
                    .get('value')) is None:
            point = next(
                (p for p in sorted(field)
                 if _finite_float((field[p].get('sample') or {})
                                  .get('value')) is not None),
                None)
        ref = save_evidence(
            ctx['evidence_dir'], 'step-bound-claim.json',
            {'claim': verdict, 'point': point, 'bound': STEP_BOUND,
             'inflow': named.get('inflow')})
        case.evidence('file', ref, 'the shared writer claim, the '
                      'documented bound, and the driven point')
        if point is None:
            return case.finish('inconclusive', 'the plant census '
                               'serves no field in-point holding a '
                               'finite float to watch')
        undriven = point == named.get('inflow')
        baseline = _plant_read(ctx, point).get('value')
        baseline_value = _finite_float(baseline)
        if baseline_value is None:
            return case.finish('inconclusive', 'the driven point '
                               + str(point) + ' reads back no finite '
                               'float baseline: '
                               + json.dumps(baseline)[:300])
        restore = (point, baseline)
        case.observe('driven point ' + str(point) + ' baseline '
                     + json.dumps(baseline)
                     + ('' if undriven else ' (element-driven — the '
                        'stored value moves as the owner steps, so '
                        'only the served finiteness is comparable)'))

        # The pair's pre-probe posture: roles and scan ticks on both
        # monitors, the field owner's io_health counters, and the
        # plant's own tick off a dt:0 step — the claim-holding probe
        # that proves the step path live while advancing nothing.
        def _pair_roles():
            reports = {name: _try_role(ctx, ctx[name])
                       for name in ('active', 'standby')}
            return reports if all(report is not None
                                  for report in reports.values()) \
                else None

        roles0 = wait_for(_pair_roles,
                          time.monotonic() + STEP_BOUND_DEADLINE)
        snaps0 = {name: _try_snapshot(ctx, ctx[name])
                  for name in ('active', 'standby')}
        tick0 = _plant_request(stream, {'op': 'step', 'dt': 0})
        ref = save_evidence(ctx['evidence_dir'],
                            'step-bound-before.json',
                            {'roles': roles0, 'ticks': {
                                name: (snaps0.get(name) or {})
                                .get('tick') for name in snaps0},
                             'plant_tick': tick0})
        case.evidence('file', ref, 'the pair and the plant ahead of '
                      'the probes')
        if not roles0:
            return case.finish('inconclusive', 'the pair\'s role '
                               'surfaces never both answered ahead '
                               'of the probes')
        if tick0.get('result') != 'stepped':
            return case.finish('inconclusive', 'a dt:0 step under '
                               'the shared claim was refused — the '
                               'step path is not live for the leg: '
                               + json.dumps(tick0)[:300])

        # The leg's own unchecked-diagnostic self-test: an audit that
        # stayed silent on — or wrongly named — a planted negative could
        # report this contract held while catching nothing, so the leg
        # proves it catches each named class before it trusts itself on
        # the rig.
        slipped = _step_bound_self_check()
        if slipped:
            return case.finish(
                'failed', 'step-bound-unchecked: the refused-probe '
                'audit stayed silent on, or wrongly named, the planted '
                'negatives: ' + ', '.join(slipped))
        case.observe('the self-check leg\'s planted negatives each named '
                     'their diagnostic')

        # The observation record the audit judges, gathered phase by
        # phase — the driven point, its baseline, whether it is
        # element-undriven, the pair's pre-probe posture.
        record = {'point': point, 'baseline': baseline,
                  'undriven': undriven, 'active': active,
                  'tracking': tracking, 'canary': tick0,
                  'snaps0': snaps0}

        # The probes under test, in the order the contract needs: the
        # ordinary over-bound advance first, so a rig predating the
        # bound absorbs this one and never sees the finding's vector.
        probes = (('the over-bound step', 'over', OVER_BOUND_DT),
                  ("the finding's 1e308 step", 'huge', HUGE_DT))
        answers = {}
        for label, slug, dt in probes:
            answer = _plant_request(stream, {'op': 'step', 'dt': dt})
            ref = save_evidence(ctx['evidence_dir'],
                                'step-bound-probe-' + slug + '.json',
                                {'dt': dt, 'answer': answer})
            case.evidence('file', ref, 'the answer to ' + label)
            name = _bound_refusal(answer)
            if name is None:
                verdict_claim = _claim_verdict(answer)
                if verdict_claim:
                    return case.finish(
                        'inconclusive', 'the shared claim never '
                        'covered the attachment — ' + label
                        + ' met the ' + verdict_claim + ' claim '
                        'verdict, not the bound contract: '
                        + json.dumps(answer)[:300])
                if answer.get('result') == 'stepped':
                    # A rig whose field still accepts an over-bound
                    # advance carries no contract for this leg to
                    # exercise. Naming it beats judging a release that
                    # predates the bound — and the finding's 1e308
                    # vector never rides it, which is why this probe
                    # runs first.
                    return case.finish(
                        'inconclusive', label + ' was applied — this '
                        'rig predates the bounded step contract: '
                        + json.dumps(answer)[:300])
                return case.finish(
                    'failed', 'step-bound-failed: ' + label
                    + ' answered off-contract — neither stepped nor '
                    'the named bound refusal: '
                    + json.dumps(answer)[:300])
            answers[label] = name
        record['answers'] = answers
        case.observe('refused by name: '
                     + ', '.join(label + ' -> ' + name
                                 for label, name in answers.items()))

        # No state change, and nothing unspellable: the driven point's
        # stored value stands (the paced field can move nothing else),
        # and the shipped client's read and census — its own decode the
        # deserialization proof — carry no `{"float":null}` frame.
        try:
            read_back = _plant_ctl(ctx, 'read', str(point))
            census = _plant_ctl(ctx, 'list')
        except Exception as exc:
            read_back, census = None, {'transport': str(exc)[:200]}
        poisoned = _poisoned_entry((census or {}).get('points') or [])
        ref = save_evidence(
            ctx['evidence_dir'], 'step-bound-decoding.json',
            {'read': read_back, 'poisoned': poisoned})
        case.evidence('file', ref, 'post-refusal deserialization: '
                      'the driven point\'s read and the census scan '
                      'for {"float":null} frames')
        record['read'] = read_back
        record['census'] = census
        _key, diagnostic, detail = _step_bound_violation(
            record, 'refusals') or (None, None, None)
        if diagnostic is not None:
            return case.finish('failed', diagnostic + ': ' + detail)
        case.observe('post-refusal reads decode finite — no '
                     '{"float":null} frame in the census, the driven '
                     'point unmoved')

        # The step path is not wedged: a finite step under the same
        # claim still advances the field, and nothing on it goes
        # non-finite afterwards.
        follow = _plant_request(stream, {'op': 'step',
                                         'dt': STEP_BOUND_FOLLOW})
        try:
            landed = _plant_ctl(ctx, 'read', str(point))
            census2 = _plant_ctl(ctx, 'list')
        except Exception as exc:
            landed = None
            census2 = {'transport': str(exc)[:200]}
        ref = save_evidence(ctx['evidence_dir'],
                            'step-bound-followup.json',
                            {'step': follow, 'read': landed,
                             'poisoned': _poisoned_entry(
                                 (census2 or {}).get('points') or [])})
        case.evidence('file', ref, 'the finite step after the refused '
                      'probes')
        record['follow'] = follow
        record['landed'] = landed
        record['census2'] = census2
        _key, diagnostic, detail = _step_bound_violation(
            record, 'followup') or (None, None, None)
        if diagnostic is not None:
            return case.finish('failed', diagnostic + ': ' + detail)
        case.observe('finite step landed — plant tick '
                     + json.dumps(tick0.get('tick')) + ' -> '
                     + json.dumps(follow.get('tick'))
                     + ', the census still finite')

        # Restore and prove the field image: the driven point's
        # baseline written back, then read again — the rig state the
        # later scenarios inherit.
        restored = _plant_request(stream, {'op': 'write',
                                           'point': point,
                                           'value': baseline})
        final = _plant_ctl(ctx, 'read', str(point))
        ref = save_evidence(ctx['evidence_dir'],
                            'step-bound-restored.json',
                            {'write': restored, 'read': final})
        case.evidence('file', ref, 'the restored driven point')
        record['restored'] = restored
        record['final'] = final
        _key, diagnostic, detail = _step_bound_violation(
            record, 'restored') or (None, None, None)
        if diagnostic is not None:
            return case.finish('failed', diagnostic + ': ' + detail)
        restore = None
        case.observe('driven point restored to ' + json.dumps(baseline))

        # The pair must be untouched: roles unmoved, both scans still
        # landing, the owner's io_health uncounted by the probes, and
        # the driven image back at baseline.
        roles1 = {name: _try_role(ctx, ctx[name])
                  for name in ('active', 'standby')}
        grown = {}
        deadline = time.monotonic() + STEP_BOUND_DEADLINE
        for name in ('active', 'standby'):
            floor = (snaps0.get(name) or {}).get('tick') or 0
            grown[name] = wait_for(
                lambda name=name, floor=floor:
                    (s.get('tick', 0) > floor and s or None)
                    if (s := _try_snapshot(ctx, ctx[name])) else None,
                deadline)
        final_image = _try_snapshot(ctx, base)
        ref = save_evidence(ctx['evidence_dir'],
                            'step-bound-pair.json',
                            {'before': {'roles': roles0,
                                        'ticks': {name: (
                                            snaps0.get(name) or {})
                                            .get('tick')
                                            for name in snaps0},
                                        'io_health': (
                                            snaps0.get(active) or {})
                                        .get('io_health')},
                             'after': {'roles': roles1,
                                       'ticks': {name: (
                                           grown.get(name) or {})
                                           .get('tick')
                                           for name in grown},
                                       'io_health': (
                                           final_image or {})
                                       .get('io_health')},
                             'driven': _point_sample(final_image or {},
                                                     point)})
        case.evidence('file', ref, 'the pair across the refused steps')
        record['roles1'] = roles1
        record['grown'] = grown
        record['image'] = final_image
        _key, diagnostic, detail = _step_bound_violation(
            record, 'pair') or (None, None, None)
        if diagnostic is not None:
            return case.finish('failed', diagnostic + ': ' + detail)
        case.observe('the pair undisturbed — ' + active + ' active, '
                     + tracking + ' tracking, both scans advancing, '
                     'io_health clean, the field image restored')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        if stream is not None:
            # Leave the rig as found: re-write the driven point's
            # baseline if the follow-up left it moved, then drop only
            # this attachment's claim hold — the owner's own holders
            # keep the standing claim.
            try:
                if restore is not None:
                    _plant_request(stream, {'op': 'write',
                                            'point': restore[0],
                                            'value': restore[1]})
            except Exception:
                pass
            try:
                _plant_request(stream, {'op': 'release_writer'})
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass