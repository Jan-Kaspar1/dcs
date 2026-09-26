"""The nonfinite_refusal acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: The nonfinite-refusal leg shares the restored pre-switch
# window the field-claim case re-establishes — the settled pair's
# pinned owner token is the standing claim its shared attachment
# joins, the refused payloads move no role and no field state, and
# the pair must still sit on its launch roles for the tune case's
# a->b switch.
RUNS_AFTER = frozenset({'scenario_field_claim'})
RUNS_BEFORE = frozenset({'scenario_parameter_tune_carryover'})


# --------------------------------------------------------------------
# The sim-net input-validation refusal contract (WW-FND-002's
# field-path honesty — the deployed-rig evidence for the #834/#866
# fixes): the shared plant must refuse a protocol-legal non-finite
# payload by name rather than apply it, because a stored non-finite
# float serves `{"float":null}` frames the protocol's own client
# cannot parse — the field corrupting for every attachment until the
# plant restarts. `{"float":1e999}` is the spelling: strict JSON the
# wire carries and a strict decoder reads as a non-finite f64, so the
# write dies at the request boundary as `invalid_request` (the
# point-level `IoError::InvalidValue` is the other named refusal a
# deserialized non-finite Value can meet), and a `"dt":1e999` step
# dies the same way — `invalid_request` is the only name a bad `dt`
# has. No other leg exercises the surface: the fault legs drive
# `inject_fault`, never Value-level invalid payloads.
#
# The leg attaches through the shared-claim seam — `ensure_writer`
# under the settled active's pinned --owner-token (ctx['plant_owner'],
# the harness claim #823 pinned) so the probes run inside the standing
# claim and the field is never preempted — and submits both payloads
# on the raw client: the shipped `dcs-plant-ctl` exposes no ensure
# under a chosen token and refuses non-finite arguments at its own
# parse, so the mutations exist only as raw protocol lines while the
# tool drives the read and list_points legs — its decode IS the
# deserialization proof, a `{"float":null}` answer being exactly what
# the shipped binary cannot print. A follow-up finite write+step must
# behave normally — no poisoned accumulator surviving it — and the
# driven point is restored before the leg releases only its own claim
# hold. Contract violations name nonfinite-refusal-failed; answers
# that disagree with the field state they report name
# nonfinite-refusal-nondeterministic.

NONFINITE_DEADLINE = 30   # bound on each settle/growth watch
NONFINITE_STEP = 0.25     # the follow-up step's finite advance


def _raw_plant_request(stream, payload):
    """One plant-protocol round trip for a pre-encoded request line —
    `_plant_request` for the spellings json.dumps cannot emit: `1e999`
    is protocol-legal JSON the strict float decoder reads as a
    non-finite f64, so it reaches the wire while a Python `inf` never
    could."""
    stream.sendall(payload + b'\n')
    line = b''
    while not line.endswith(b'\n'):
        chunk = stream.recv(PLANT_MAX_MESSAGE)
        if not chunk:
            raise ConnectionError('the plant server closed the '
                                  'connection mid-request')
        line += chunk
        if len(line) > PLANT_MAX_MESSAGE:
            raise ConnectionError('a plant response exceeded the '
                                  'protocol message bound')
    return json.loads(line)


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
    poisoning shape — or None while every served float decodes
    finite."""
    for entry in entries:
        value = (entry.get('sample') or {}).get('value')
        is_float, raw = _float_payload(value)
        if is_float and (not isinstance(raw, (int, float))
                         or isinstance(raw, bool)
                         or not math.isfinite(raw)):
            return entry
    return None


def _write_refusal(response):
    """The named refusal a non-finite write's answer carries:
    'invalid_request' when the payload dies at the protocol's strict
    decode, 'io.invalid_value' when it parses and the value-level
    contract refuses it — or None on any other answer."""
    error = (response or {}).get('error')
    if not isinstance(error, dict):
        return None
    if error.get('kind') == 'invalid_request':
        return 'invalid_request'
    inner = error.get('error')
    if error.get('kind') == 'io' and isinstance(inner, dict) \
            and 'invalid_value' in inner:
        return 'io.invalid_value'
    return None


def _step_refusal(response):
    """The named refusal a non-finite `dt` step's answer carries —
    'invalid_request' is the only name the contract has for it — or
    None."""
    error = (response or {}).get('error')
    if isinstance(error, dict) \
            and error.get('kind') == 'invalid_request':
        return 'invalid_request'
    return None


def _claim_verdict(response):
    """A mutation answer's claim-state refusal — 'fenced' or
    'unclaimed', the io-carried fenced write verdict included — or
    None: the answers that mean the probes met the claim contract
    rather than the value contract under test."""
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


def _ctl_decodes(response, want):
    """Whether a `dcs-plant-ctl` answer is the named result — the
    shipped client's own decode succeeding. A refused invocation comes
    back error-shaped; a poisoned `{"float":null}` frame is the decode
    failure its nonzero exit carries."""
    return isinstance(response, dict) \
        and response.get('result') == want


def scenario_nonfinite_refusal(ctx):
    """A shared-claim attachment's non-finite write and step meet the
    protocol's named refusals, leave every served frame decodable and
    finite, and cost the pair nothing — then a finite write+step lands
    and the driven point restores."""
    case = Case('nonfinite-refusal',
                'Non-finite write and step payloads refuse by name',
                'with the deployed pair settled and tracking, a '
                'plant-protocol attachment sharing the active\'s '
                'pinned writer claim submits a write carrying a '
                'non-finite float and a step carrying a non-finite '
                'dt: each answers the protocol\'s named refusal '
                'instead of applying, subsequent read and list_points '
                'responses keep deserializing finite values for every '
                'driven point — no {"float":null} poisoning survives '
                'a follow-up finite write+step — and both peers\' '
                'scans, served snapshots, and roles are unaffected, '
                'with the driven field point restored')
    stream = None
    restore = None        # (point, baseline Value dict) once known
    try:
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + NONFINITE_DEADLINE)
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
                            time.monotonic() + NONFINITE_DEADLINE)
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

        # The seam split, as the shared-claim legs document it: the
        # census and the reads ride the shipped dcs-plant-ctl — the
        # tool's own decode is the leg's deserialization proof —
        # while the claim ensure and the mutations it guards stay on
        # the raw client. The tool exposes no ensure_writer under a
        # chosen owner token, and its argument parse refuses
        # non-finite values before any wire exchange, so the payloads
        # under test exist only as raw protocol lines.
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
        # any field in-point holding a finite float. A point an
        # element owns answers the follow-up write's read-back only
        # weakly (the next plant step rewrites it), so the strict
        # landed-equality check is the forcing input's alone.
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
            ctx['evidence_dir'], 'nonfinite-refusal-claim.json',
            {'claim': verdict, 'point': point,
             'inflow': named.get('inflow')})
        case.evidence('file', ref, 'the shared writer claim and the '
                      'driven point')
        if point is None:
            return case.finish('inconclusive', 'the plant census '
                               'serves no field in-point holding a '
                               'finite float to write')
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
                     + ('' if undriven else ' (element-driven — a '
                        'weaker follow-up read-back applies)'))

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
                          time.monotonic() + NONFINITE_DEADLINE)
        snaps0 = {name: _try_snapshot(ctx, ctx[name])
                  for name in ('active', 'standby')}
        tick0 = _plant_request(stream, {'op': 'step', 'dt': 0})
        ref = save_evidence(ctx['evidence_dir'],
                            'nonfinite-refusal-before.json',
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

        # The probes under test: protocol-legal non-finite payloads
        # the tool cannot spell, sent as raw lines. Each must answer
        # the named refusal — never apply.
        write_answer = _raw_plant_request(
            stream,
            ('{"op":"write","point":' + str(point)
             + ',"value":{"float":1e999}}').encode())
        step_answer = _raw_plant_request(
            stream, b'{"op":"step","dt":1e999}')
        ref = save_evidence(ctx['evidence_dir'],
                            'nonfinite-refusal-probes.json',
                            {'point': point, 'write': write_answer,
                             'step': step_answer})
        case.evidence('file', ref, 'the non-finite write and step '
                      'answers')
        if write_answer.get('result') == 'done':
            return case.finish(
                'failed', 'nonfinite-refusal-failed: the non-finite '
                'float write was applied — the plant answered '
                + json.dumps(write_answer)[:300])
        write_name = _write_refusal(write_answer)
        if write_name is None:
            if _claim_verdict(write_answer):
                return case.finish(
                    'inconclusive', 'the shared claim never covered '
                    'the attachment — the write probe met the '
                    'fencing verdict '
                    + json.dumps(write_answer)[:300]
                    + ', not the value contract')
            return case.finish(
                'failed', 'nonfinite-refusal-failed: the non-finite '
                'float write answered off-contract — neither applied '
                'nor the named refusal: '
                + json.dumps(write_answer)[:300])
        if step_answer.get('result') == 'stepped':
            return case.finish(
                'failed', 'nonfinite-refusal-failed: the non-finite '
                'dt step was applied — the plant answered '
                + json.dumps(step_answer)[:300])
        step_name = _step_refusal(step_answer)
        if step_name is None:
            if _claim_verdict(step_answer):
                return case.finish(
                    'inconclusive', 'the shared claim never covered '
                    'the attachment — the step probe met the '
                    'fencing verdict '
                    + json.dumps(step_answer)[:300]
                    + ', not the value contract')
            return case.finish(
                'failed', 'nonfinite-refusal-failed: the non-finite '
                'dt step answered off-contract — neither applied nor '
                'the named refusal: '
                + json.dumps(step_answer)[:300])
        case.observe('refused by name: write ' + write_name
                     + ', step ' + step_name)

        # The decode half: the refused payloads must have stored
        # nothing unspellable. The shipped client's read of the
        # driven point and census of the field are the proof — an
        # answer the tool cannot print is the defect itself.
        try:
            read_back = _plant_ctl(ctx, 'read', str(point))
            census = _plant_ctl(ctx, 'list')
        except Exception as exc:
            read_back, census = None, {'transport': str(exc)[:200]}
        ref = save_evidence(
            ctx['evidence_dir'], 'nonfinite-refusal-decoding.json',
            {'read': read_back, 'poisoned':
             _poisoned_entry((census or {}).get('points') or [])})
        case.evidence('file', ref, 'post-refusal deserialization: '
                      'the driven point\'s read and the census scan '
                      'for {"float":null} frames')
        if not _ctl_decodes(read_back, 'sample'):
            return case.finish(
                'failed', 'nonfinite-refusal-failed: the refused '
                'payloads left state the shipped client cannot '
                'deserialize — read on point ' + str(point)
                + ' answered ' + json.dumps(read_back)[:300])
        served = (read_back.get('sample') or {}).get('value')
        if _float_payload(served)[0] \
                and _finite_float(served) is None:
            return case.finish(
                'failed', 'nonfinite-refusal-failed: the driven '
                'point serves a non-finite float — the '
                '{"float":null} poisoning the refusal exists to '
                'prevent: ' + json.dumps(served)[:300])
        # An element-driven point legitimately moves between reads —
        # the standing owner's steps keep integrating it — so the
        # stored-value equality the refusal promises is checkable only
        # on the undriven forcing input.
        if undriven and served != baseline:
            return case.finish(
                'failed', 'nonfinite-refusal-nondeterministic: the '
                'write answered ' + write_name + ' yet the stored '
                'value moved — ' + json.dumps(baseline) + ' -> '
                + json.dumps(served)[:300])
        poisoned = _poisoned_entry((census or {}).get('points') or [])
        if not _ctl_decodes(census, 'points'):
            return case.finish(
                'failed', 'nonfinite-refusal-failed: the post-refusal '
                'census no longer deserializes for the shipped '
                'client: ' + json.dumps(census)[:300])
        if poisoned is not None:
            return case.finish(
                'failed', 'nonfinite-refusal-failed: point '
                + str(poisoned.get('point')) + ' serves a non-finite '
                'sample the wire cannot spell — the refused payloads '
                'still poisoned the field: '
                + json.dumps(poisoned.get('sample'))[:300])
        case.observe('post-refusal reads decode finite — no '
                     '{"float":null} frame in the census')

        # The follow-up: a finite write+step under the same claim must
        # behave normally — the proof no poisoned accumulator or
        # wedged claim survived the refused payloads.
        follow = baseline_value + 1.0
        if not math.isfinite(follow) or follow == baseline_value:
            follow = math.nextafter(baseline_value, math.inf)
        fin_write = _plant_request(
            stream, {'op': 'write', 'point': point,
                     'value': {'float': follow}})
        fin_step = _plant_request(
            stream, {'op': 'step', 'dt': NONFINITE_STEP})
        try:
            landed = _plant_ctl(ctx, 'read', str(point))
            census2 = _plant_ctl(ctx, 'list')
        except Exception as exc:
            landed = None
            census2 = {'transport': str(exc)[:200]}
        ref = save_evidence(
            ctx['evidence_dir'], 'nonfinite-refusal-followup.json',
            {'write': fin_write, 'step': fin_step,
             'read': landed,
             'poisoned': _poisoned_entry(
                 (census2 or {}).get('points') or [])})
        case.evidence('file', ref, 'the finite write+step after the '
                      'refused payloads')
        if fin_write.get('result') != 'done':
            return case.finish(
                'failed', 'nonfinite-refusal-failed: a finite write '
                'under the same claim was refused after the named '
                'refusals — the probes wedged the field: '
                + json.dumps(fin_write)[:300])
        if fin_step.get('result') != 'stepped':
            return case.finish(
                'failed', 'nonfinite-refusal-failed: a finite step '
                'under the same claim was refused after the named '
                'refusals: ' + json.dumps(fin_step)[:300])
        if not isinstance(fin_step.get('tick'), int) \
                or fin_step['tick'] <= (tick0.get('tick') or 0):
            return case.finish(
                'failed', 'nonfinite-refusal-nondeterministic: the '
                'finite step answered stepped but the plant tick '
                'never advanced — ' + json.dumps(tick0.get('tick'))
                + ' -> ' + json.dumps(fin_step.get('tick')))
        if not _ctl_decodes(landed, 'sample'):
            return case.finish(
                'failed', 'nonfinite-refusal-failed: the post-step '
                'read no longer deserializes for the shipped '
                'client: ' + json.dumps(landed)[:300])
        landed_value = (landed.get('sample') or {}).get('value')
        if undriven and landed_value != {'float': follow}:
            return case.finish(
                'failed', 'nonfinite-refusal-nondeterministic: the '
                'applied finite write never landed — the driven '
                'point reads ' + json.dumps(landed_value)[:300]
                + ', not ' + json.dumps({'float': follow}))
        if _float_payload(landed_value)[0] \
                and _finite_float(landed_value) is None:
            return case.finish(
                'failed', 'nonfinite-refusal-failed: the follow-up '
                'write+step left the driven point non-finite: '
                + json.dumps(landed_value)[:300])
        poisoned = _poisoned_entry((census2 or {}).get('points') or [])
        if not _ctl_decodes(census2, 'points'):
            return case.finish(
                'failed', 'nonfinite-refusal-failed: the post-step '
                'census no longer deserializes for the shipped '
                'client: ' + json.dumps(census2)[:300])
        if poisoned is not None:
            return case.finish(
                'failed', 'nonfinite-refusal-failed: the follow-up '
                'write+step surfaced a non-finite accumulator — '
                'point ' + str(poisoned.get('point'))
                + ' serves ' + json.dumps(poisoned.get('sample'))
                [:300])
        case.observe('finite write+step behaved normally — plant '
                     'tick ' + json.dumps(tick0.get('tick')) + ' -> '
                     + json.dumps(fin_step.get('tick')))

        # Restore and prove the field image: the driven point's
        # baseline written back, then read again — the rig state the
        # later scenarios inherit.
        restored = _plant_request(
            stream, {'op': 'write', 'point': point,
                     'value': baseline})
        final = _plant_ctl(ctx, 'read', str(point))
        ref = save_evidence(ctx['evidence_dir'],
                            'nonfinite-refusal-restored.json',
                            {'write': restored, 'read': final})
        case.evidence('file', ref, 'the restored driven point')
        if restored.get('result') != 'done':
            return case.finish(
                'failed', 'nonfinite-refusal-failed: the restore '
                'write was refused — the field did not come back: '
                + json.dumps(restored)[:300])
        restore = None
        if not _ctl_decodes(final, 'sample'):
            return case.finish(
                'failed', 'nonfinite-refusal-failed: the restored '
                'point no longer deserializes for the shipped '
                'client: ' + json.dumps(final)[:300])
        if undriven \
                and (final.get('sample') or {}).get('value') \
                != baseline:
            return case.finish(
                'failed', 'nonfinite-refusal-nondeterministic: the '
                'restore write answered done but the point reads '
                + json.dumps(
                    (final.get('sample') or {}).get('value'))[:300]
                + ', not the baseline ' + json.dumps(baseline))
        if not undriven and _float_payload(
                (final.get('sample') or {}).get('value'))[0] \
                and _finite_float(
                    (final.get('sample') or {}).get('value')) is None:
            return case.finish(
                'failed', 'nonfinite-refusal-failed: the restored '
                'point serves a non-finite float: '
                + json.dumps(
                    (final.get('sample') or {}).get('value'))[:300])
        case.observe('driven point restored to '
                     + json.dumps(baseline))

        # The pair must be untouched: roles unmoved, both scans still
        # landing, the owner's io_health uncounted by the probes, and
        # — for the undriven forcing input — the served image back at
        # baseline.
        roles1 = {name: _try_role(ctx, ctx[name])
                  for name in ('active', 'standby')}
        grown = {}
        deadline = time.monotonic() + NONFINITE_DEADLINE
        for name in ('active', 'standby'):
            floor = (snaps0.get(name) or {}).get('tick') or 0
            grown[name] = wait_for(
                lambda name=name, floor=floor:
                    (s.get('tick', 0) > floor and s or None)
                    if (s := _try_snapshot(ctx, ctx[name])) else None,
                deadline)
        final_image = _try_snapshot(ctx, base)
        ref = save_evidence(
            ctx['evidence_dir'], 'nonfinite-refusal-pair.json',
            {'before': {'roles': roles0,
                        'ticks': {name: (snaps0.get(name) or {})
                                  .get('tick')
                                  for name in snaps0},
                        'io_health': (snaps0.get(active) or {})
                                     .get('io_health')},
             'after': {'roles': roles1,
                       'ticks': {name: (grown.get(name) or {})
                                 .get('tick')
                                 for name in grown},
                       'io_health': (final_image or {})
                                    .get('io_health')},
             'driven': _point_sample(final_image or {}, point)})
        case.evidence('file', ref, 'the pair across the refused '
                      'payloads')
        if (roles1.get(active) or {}).get('role') != 'active' \
                or _settled_active(ctx) != active:
            return case.finish(
                'failed', 'nonfinite-refusal-failed: the refused '
                'payloads moved the active role: '
                + json.dumps(roles1)[:300])
        if (roles1.get(tracking) or {}).get('role') != 'standby':
            return case.finish(
                'failed', 'nonfinite-refusal-failed: the refused '
                'payloads moved the tracking peer\'s role: '
                + json.dumps(roles1)[:300])
        for name in ('active', 'standby'):
            if grown.get(name) is None:
                return case.finish(
                    'failed', 'nonfinite-refusal-failed: ' + name
                    + '\'s scans stalled under the refused payloads '
                    '— tick ' + json.dumps(
                        (snaps0.get(name) or {}).get('tick'))
                    + ' never advanced')
        health0 = (snaps0.get(active) or {}).get('io_health') or {}
        health1 = (final_image or {}).get('io_health') or {}
        for key in ('failed_reads', 'failed_writes'):
            if (health1.get(key) or 0) > (health0.get(key) or 0):
                return case.finish(
                    'failed', 'nonfinite-refusal-failed: the refused '
                    'payloads counted against the owner\'s io_health: '
                    + json.dumps(health1)[:300])
        if undriven:
            image = _point_value(final_image or {}, point)
            if image != baseline_value:
                return case.finish(
                    'failed', 'nonfinite-refusal-failed: the field '
                    'image did not restore — the active serves '
                    + json.dumps(image)[:300] + ' for point '
                    + str(point) + ', not the baseline '
                    + json.dumps(baseline_value))
        case.observe('the pair undisturbed — ' + active + ' active, '
                     + tracking + ' tracking, both scans advancing, '
                     'io_health clean, the field image restored')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        if stream is not None:
            # Leave the rig as found: re-write the driven point's
            # baseline if the follow-up value still stands, then drop
            # only this attachment's claim hold — the owner's own
            # holders keep the standing claim.
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
