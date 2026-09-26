"""The bare_point_restamp acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the bare-point-restamp case sits in the same restored
# pre-switch window the stale-freshness leg's writer-stop
# re-establishes — it stops and restarts the same field writer under
# the settled pair's pinned owner token, so the pair must sit on its
# launch roles for the tune case's a->b switch.
RUNS_BEFORE = frozenset({'scenario_parameter_tune_carryover'})


# --------------------------------------------------------------------
# The bare-channel restamping contract (WW-OPS-003's freshness
# honesty — the per-revision lane evidence for #988): a bare sim-net
# channel point — one no loopback routes onto and no dynamics element
# owns — is re-stamped each plant step like a scanned input card.
# Freshness on the field side means the plant is still scanning, not
# that the value changed, so while the field owner steps, the bare
# point's served stamp advances with the plant tick; only a stopped
# plant lets the stamp age. The leg picks a named bare channel-bound
# field input out of the served SignalIndex and the plant census —
# `inflow`, the dynamics document's forcing input nothing drives,
# first — proves bareness by writing a changed value a driver would
# overwrite on the next step and watching it hold, then drives the
# plant forward through the lane's stepping channel: explicit `step`
# requests on the raw client under the shared writer claim the
# field-claim and nonfinite legs attach with (ctx['plant_owner'] pins
# the settled active's --owner-token, ensure_writer grants
# claimed_shared — the designed test-harness claim). Each driven step
# must advance the served stamp rather than leave it holding its
# initial stamp; the writer-stop seam the stale-freshness leg (0600)
# uses then freezes the shared plant's stepping — no surviving peer
# steps it — and the bare point's stamp must stop advancing while a
# dynamics-driven point's stamp keeps its last driven value: the
# honesty contrast #988 restores, a stepping plant re-stamping and a
# stopped plant freezing rather than a stamp that lied either way.
# The outage is bounded under the armed failover budget so the
# writer's restart lands first — the same bound 0600 rides; should
# the standby's budget fire first the promoted run reclaims the
# writer and the resumed stepping is the documented recovery path,
# which the leg reports rather than treating as rig damage. Contract
# violations name bare-point-restamp-failed; answers that disagree
# with the field state they report — a served stamp rewinding or a
# landed step the stamp lags — name
# bare-point-restamp-nondeterministic. A rig that is unreachable,
# that predates the restamping contract, or that exposes no bare
# channel-bound input reports inconclusive.

RESTAMP_SETTLE = 30        # bound on the pair reporting settled
RESTAMP_POLL = 0.2         # cadence polling the frozen plant
RESTAMP_STEPS = 4          # driven plant steps the advance window spans
RESTAMP_STEP_DT = 0.25     # the driven step's simulated advance
RESTAMP_HOLD = 4           # static reads proving the frozen plant
RESTAMP_MIN_STATIC = 2     # statics a promotion must still leave
RESTAMP_FREEZE_DEADLINE = 20   # backstop on the freeze window
RESTAMP_RESUME_DEADLINE = 45   # bound on the stamp resuming post-restart
RESTAMP_RETURN_DEADLINE = 45   # bound on the restarted writer's return

# The probe pools out of the served SignalIndex: a named bare
# channel-bound field input — `inflow`, the deployed dynamics'
# forcing input no element owns, before the contact points no
# element drives — and a dynamics-driven contrast stamped by its own
# rule. The census's field in-points decide whether the named point
# is channel-bound at all.
RESTAMP_BARE_NAMES = ('inflow', 'power-fail', 'p101-run', 'p102-run',
                      'p101-thermal', 'p102-thermal', 'p101-moisture',
                      'p102-moisture')
RESTAMP_DRIVEN_NAMES = ('level-primary', 'level-backup', 'net-flow',
                        'p101-draw', 'p102-draw')


def _restamp_sentinel(value):
    """A changed value of a served Value dict's own kind — the probe
    write an element owner would overwrite on the next plant step."""
    if not isinstance(value, dict):
        return None
    if 'float' in value:
        raw = value.get('float')
        changed = raw + 1.0 if isinstance(raw, (int, float)) \
            and not isinstance(raw, bool) else 1.0
        return {'float': changed} if math.isfinite(changed) \
            else {'float': 1.0}
    if 'bool' in value:
        return {'bool': not value.get('bool')}
    if 'int' in value:
        raw = value.get('int')
        return {'int': raw + 1 if isinstance(raw, int)
                and not isinstance(raw, bool) else 1}
    return None


def _stamp(sample):
    """A served sample's plant tick when it is an int, else None —
    the stamp the restamping contract answers for."""
    tick = (sample or {}).get('tick')
    return tick if isinstance(tick, int) \
        and not isinstance(tick, bool) else None


def _restamp_read(ctx, point):
    """(tick, value) of the plant's stored sample for `point`, or
    None — a dropped read is one lost poll, never the leg's verdict."""
    sample = _field_sample(ctx, point)
    if sample is None:
        return None
    return _stamp(sample), sample.get('value')


def scenario_bare_point_restamp(ctx):
    """Step the shared plant under the settled active's shared
    writer claim and assert a named bare channel-bound input's served
    stamp advances with each driven plant step; freezing stepping by
    stopping the writer must freeze the bare stamp while a
    dynamics-driven point keeps its last driven stamp, and the
    restarted writer's resumed stepping must advance the bare stamp
    again."""
    case = Case('bare-point-restamp',
                'A bare sim-net channel re-stamps with the stepped '
                'plant and freezes with it',
                'with the deployed pair settled and tracking, a named '
                'bare channel-bound field input — no loopback or '
                'dynamics element owns it — is driven forward through '
                'the lane\'s shared-claim stepping channel: its '
                'served sample tick advances each driven plant step '
                'rather than holding its initial stamp; stopping the '
                'writer-holding controller freezes the plant\'s '
                'stepping, the bare point\'s stamp stops advancing '
                'while a dynamics-driven point\'s stamp keeps its '
                'last driven value, and the restarted writer\'s '
                'resumed stepping advances the bare stamp again')
    stream = None
    restore = None        # (point, baseline Value dict) once written
    try:
        stop = ctx.get('stop_controller')
        start = ctx.get('start_controller')
        if stop is None or start is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no controller stop/start '
                               'action — the stepping-freeze '
                               'induction has no documented seam')
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + RESTAMP_SETTLE)
        if active is None:
            reachable = any(
                _try_role(ctx, ctx[name]) is not None
                for name in ('active', 'standby') if ctx.get(name))
            return case.finish(
                'failed' if reachable else 'inconclusive',
                'no peer reports role=active' if reachable
                else 'the rig is unreachable')
        peer = 'standby' if active == 'active' else 'active'
        base, peer_base = ctx[active], ctx[peer]
        tracking = wait_for(lambda: _tracking_peer(ctx, active),
                            time.monotonic() + RESTAMP_SETTLE,
                            interval=RESTAMP_POLL)
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
        case.observe('seam=writer-stop: stop ' + active + ' (' + base
                     + '), observe ' + peer + ' (' + peer_base + ')'
                     + (', failover budget '
                        + str(ctx.get('failover_misses')) + ' misses'
                        if ctx.get('failover_misses') else ''))

        # The probe pair out of the served index and the field's own
        # census: a named bare channel-bound input and a named
        # dynamics-driven one — the two stamp rules the contract
        # contrasts.
        _, signals = http_json('GET', base + '/signals')
        try:
            field = _field_inputs(ctx)
        except Exception as exc:
            return case.finish('inconclusive', 'the plant census '
                               'never answered: ' + str(exc)[:300])
        named = {entry.get('name'): entry
                 for entry in (signals or {}).get('points', [])}
        probes = {}
        for key, names in (('bare', RESTAMP_BARE_NAMES),
                           ('driven', RESTAMP_DRIVEN_NAMES)):
            probes[key] = next(
                ((name, named[name].get('point'))
                 for name in names
                 if named.get(name) is not None
                 and named[name].get('point') in field), None)
        ref = save_evidence(
            ctx['evidence_dir'], 'bare-point-restamp-probes.json',
            {'bare': probes['bare'], 'driven': probes['driven'],
             'field': sorted(field)})
        case.evidence('file', ref, 'the named probe points against '
                      'the field census')
        if probes['bare'] is None:
            return case.finish('inconclusive', 'the rig exposes no '
                               'bare channel-bound field input — no '
                               'named candidate is a served field '
                               'in-point')
        if probes['driven'] is None:
            return case.finish('inconclusive', 'the rig exposes no '
                               'dynamics-driven field input for the '
                               'stamp-rule contrast')
        bare_name, bare = probes['bare']
        driven_name, driven = probes['driven']
        case.observe('probes: bare ' + bare_name + ' point '
                     + str(bare) + '; driven ' + driven_name
                     + ' point ' + str(driven))

        # The seam split, as the shared-claim legs document it: the
        # claim ensure and the writes/steps it guards stay on the raw
        # client — the shipped tool exposes no ensure_writer under a
        # chosen token — while the reads ride dcs-plant-ctl, open to
        # every attachment.
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

        # The bareness proof: a written value an element owner would
        # overwrite on the next step must hold — the named point is a
        # bare channel only while nothing drives it.
        baseline_b = _plant_read(ctx, bare)
        baseline_v = baseline_b.get('value')
        sentinel = _restamp_sentinel(baseline_v)
        if sentinel is None:
            return case.finish('inconclusive', 'the bare probe '
                               'serves no writable value kind: '
                               + json.dumps(baseline_b)[:300])
        wrote = _plant_request(stream, {'op': 'write', 'point': bare,
                                        'value': sentinel})
        if wrote.get('result') != 'done':
            return case.finish('inconclusive', 'the bareness probe '
                               'write was refused under the shared '
                               'claim: ' + json.dumps(wrote)[:300])
        restore = (bare, baseline_v)
        probe_step = _plant_request(stream, {'op': 'step',
                                             'dt': RESTAMP_STEP_DT})
        if probe_step.get('result') != 'stepped':
            return case.finish('inconclusive', 'the bareness '
                               'probe\'s plant step answered '
                               'off-contract: '
                               + json.dumps(probe_step)[:300])
        held = _plant_read(ctx, bare)
        if held.get('value') != sentinel:
            return case.finish('inconclusive', 'the named point '
                               + bare_name + ' reads '
                               + json.dumps(held.get('value'))[:200]
                               + ' after a driven step, not the '
                               'written ' + json.dumps(sentinel)[:200]
                               + ' — an element owns it: the rig '
                               'exposes no bare channel-bound input')
        restored = _plant_request(
            stream, {'op': 'write', 'point': bare,
                     'value': baseline_v})
        if restored.get('result') != 'done':
            return case.finish('inconclusive', 'the baseline restore '
                               'write was refused under the shared '
                               'claim: ' + json.dumps(restored)[:300])
        restore = None
        case.observe('bareness proven: the written value held '
                     'across a driven step — no element owns '
                     + bare_name)

        # The stepped window: each driven plant step must advance the
        # bare point's served stamp — never leave it holding an old
        # stamp — while the dynamics-driven contrast advances by its
        # own element writes.
        stamp_b = _stamp(held)
        read_d = _restamp_read(ctx, driven)
        stamp_d = read_d[0] if read_d else None
        rows = []
        step_error = None
        for _round in range(RESTAMP_STEPS):
            answer = _plant_request(stream, {'op': 'step',
                                             'dt': RESTAMP_STEP_DT})
            got_b = _restamp_read(ctx, bare)
            got_d = _restamp_read(ctx, driven)
            rows.append({'answer': answer.get('result'),
                         'stepped': answer.get('tick'),
                         'bare': got_b[0] if got_b else None,
                         'driven': got_d[0] if got_d else None,
                         'driven_value': got_d[1] if got_d else None,
                         'dropped': got_b is None or got_d is None})
            if answer.get('result') != 'stepped':
                step_error = answer
                break
        kept = [row for row in rows if not row['dropped']]
        ref = save_evidence(
            ctx['evidence_dir'], 'bare-point-restamp-steps.json',
            {'bare': bare, 'driven': driven, 'baseline': {
                'bare': stamp_b, 'driven': stamp_d}, 'rows': rows})
        case.evidence('file', ref, 'the per-step served stamps')
        if step_error is not None:
            kind = _probe_error(step_error)
            if kind in ('fenced', 'unclaimed'):
                return case.finish(
                    'inconclusive', 'the stepping channel\'s claim '
                    'broke mid-window — the driven step met the '
                    'fencing verdict: ' + json.dumps(step_error)[:300])
            return case.finish(
                'failed', 'bare-point-restamp-failed: a driven plant '
                'step answered off-contract: '
                + json.dumps(step_error)[:300])

        ticks_b = [stamp_b] + [row['bare'] for row in kept]
        ticks_d = [stamp_d] + [row['driven'] for row in kept]
        if len(kept) < 2:
            return case.finish('inconclusive', 'the stepped window '
                               'never filled — dropped reads left '
                               'nothing to judge')
        if any(not isinstance(tick, int) for tick in ticks_b + ticks_d):
            return case.finish(
                'failed', 'bare-point-restamp-nondeterministic: a '
                'served sample carried no integer stamp: '
                + json.dumps(rows)[:400])
        rewound = any(ticks_b[i] < ticks_b[i - 1]
                      for i in range(1, len(ticks_b))) or any(
            ticks_d[i] < ticks_d[i - 1]
            for i in range(1, len(ticks_d)))
        if rewound:
            return case.finish(
                'failed', 'bare-point-restamp-nondeterministic: a '
                'served stamp rewound across the stepped window: '
                + json.dumps({'bare': ticks_b,
                              'driven': ticks_d})[:400])
        if ticks_d[-1] <= ticks_d[0]:
            return case.finish('inconclusive', 'the driven '
                               'contrast\'s stamp never advanced — '
                               'the stepping channel moved nothing '
                               'to attribute the bare stamp to')
        if len(set(ticks_b[1:])) == 1:
            return case.finish('inconclusive', 'the bare point held '
                               'its stamp across the whole stepped '
                               'window while the driven contrast '
                               'advanced — the rig predates the '
                               'restamping contract')
        gaps = [i for i in range(1, len(ticks_b))
                if ticks_b[i] == ticks_b[i - 1]]
        if gaps:
            return case.finish(
                'failed', 'bare-point-restamp-failed: the bare '
                'point\'s served stamp held across a driven plant '
                'step at window index ' + str(gaps[0]) + ': '
                + json.dumps(ticks_b)[:300])
        lagged = next(
            (row for row in kept
             if isinstance(row['stepped'], int)
             and row['bare'] < row['stepped']), None)
        if lagged is not None:
            return case.finish(
                'failed', 'bare-point-restamp-nondeterministic: the '
                'bare point\'s served stamp ' + str(lagged['bare'])
                + ' lagged the landed step\'s tick '
                + str(lagged['stepped']))
        case.observe('stepped window: bare stamp ' + str(ticks_b[0])
                     + ' -> ' + str(ticks_b[-1]) + ' over '
                     + str(len(rows)) + ' driven steps; driven '
                     + str(ticks_d[0]) + ' -> ' + str(ticks_d[-1]))

        # The freeze: drop this attachment's claim hold first — a
        # lingering hold marked a controller's would refuse the
        # standby's conditional takeover if the failover budget
        # fired — then stop the writer-holding controller, the
        # stale-freshness leg's seam. No surviving peer steps the
        # plant: the bare point's re-stamp stops at the last stepped
        # tick while the dynamics-driven point keeps its last driven
        # stamp — both honest, either advance an induction that never
        # took.
        try:
            _plant_request(stream, {'op': 'release_writer'})
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass
        stream = None
        floor_b, floor_d = ticks_b[-1], ticks_d[-1]
        floor_dv = kept[-1]['driven_value']
        try:
            stop(active)
        except Exception as exc:
            return case.finish('inconclusive', 'the writer-stop '
                               'induction never completed: '
                               + str(exc)[:300])
        case.observe('writer stopped — polling the plant for the '
                     'frozen stamps')

        static = 0
        moved = None
        promoted = False
        freeze_obs = []
        wall = time.monotonic() + RESTAMP_FREEZE_DEADLINE
        while time.monotonic() < wall and static < RESTAMP_HOLD:
            report = _try_role(ctx, peer_base)
            if report is not None \
                    and report.get('role') in ('promoting', 'active'):
                promoted = True
                break
            got_b = _restamp_read(ctx, bare)
            got_d = _restamp_read(ctx, driven)
            if got_b is None or got_d is None:
                time.sleep(RESTAMP_POLL)
                continue
            freeze_obs.append({'bare': got_b[0], 'driven': got_d[0],
                               'driven_value': got_d[1],
                               'role': (report or {}).get('role')})
            if got_b[0] is None or got_d[0] is None:
                moved = 'unstamped'
                break
            if got_b[0] < floor_b or got_d[0] < floor_d:
                moved = 'rewound'
                break
            if got_b[0] != floor_b or got_d[0] != floor_d \
                    or got_d[1] != floor_dv:
                moved = 'advanced'
                break
            static += 1
            time.sleep(RESTAMP_POLL)
        case.observe('freeze: ' + str(static)
                     + ' static reads'
                     + (', moved: ' + moved if moved else '')
                     + (', peer promoted' if promoted else ''))

        # Restore inside the failover bound: the restarted writer
        # resumes its persisted run, reclaims the plant, and steps it
        # again — unless the peer's armed promotion reclaimed the
        # field first, the documented bound 0600 reports; the resumed
        # stepping is what must advance the bare stamp.
        try:
            start(active)
        except Exception as exc:
            return case.finish('inconclusive', 'the writer restart '
                               'never completed: ' + str(exc)[:300])
        case.observe('writer restart issued')

        def resumed():
            _try_role(ctx, base)
            _try_role(ctx, peer_base)
            got = _restamp_read(ctx, bare)
            if got is None or got[0] is None or got[0] <= floor_b:
                return None
            return {'tick': got[0]}

        back = wait_for(resumed,
                        time.monotonic() + RESTAMP_RESUME_DEADLINE,
                        interval=RESTAMP_POLL)
        ref = save_evidence(
            ctx['evidence_dir'], 'bare-point-restamp-freeze.json',
            {'seam': 'writer-stop', 'floor': {
                'bare': floor_b, 'driven': floor_d,
                'driven_value': floor_dv},
             'static': static, 'moved': moved, 'promoted': promoted,
             'observations': freeze_obs, 'resumed': back})
        case.evidence('file', ref, 'per-poll stamps through the '
                      'freeze and the resume read')

        if moved == 'rewound':
            return case.finish(
                'failed', 'bare-point-restamp-nondeterministic: a '
                'served stamp rewound across the frozen plant: '
                + json.dumps(freeze_obs)[:400])
        if moved == 'unstamped':
            return case.finish(
                'failed', 'bare-point-restamp-nondeterministic: a '
                'served sample carried no integer stamp through '
                'the freeze: ' + json.dumps(freeze_obs)[:400])
        if moved:
            return case.finish(
                'inconclusive', 'the writer-stop induction never '
                'took effect — the plant\'s stepping kept advancing '
                'the served stamps, so no freeze honesty can be '
                'attributed')
        if promoted and static < RESTAMP_MIN_STATIC:
            return case.finish(
                'inconclusive', 'the peer\'s failover promotion '
                'reclaimed the field before the frozen stamps could '
                'be observed — no freeze window to judge')
        if not promoted and static < RESTAMP_HOLD:
            return case.finish(
                'inconclusive', 'the freeze window never produced '
                + str(RESTAMP_HOLD) + ' static reads — the plant '
                'stopped answering before the hold was proven')
        if back is None:
            note = 'the bare point\'s served stamp never resumed '
            if promoted:
                note += ('advancing — the peer\'s failover-budget '
                         'self-promotion reclaimed the writer, but '
                         'the promoted run\'s stepping left the '
                         'stamp frozen past '
                         + str(RESTAMP_RESUME_DEADLINE) + 's')
            else:
                note += ('advancing inside '
                         + str(RESTAMP_RESUME_DEADLINE)
                         + 's of the writer\'s restart')
            return case.finish('failed',
                               'bare-point-restamp-failed: ' + note)
        case.observe('the bare stamp resumed advancing at plant '
                     'tick ' + str(back.get('tick'))
                     + (' — the peer\'s failover-budget '
                        'self-promotion reclaimed the writer and '
                        'resumed stepping on the promoted run'
                        if promoted else
                        ' — the restarted writer\'s stepping '
                        'restored it'))

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
                return report if report.get('role') == 'active' \
                    else None

            returned = wait_for(
                serving_again,
                time.monotonic() + RESTAMP_RETURN_DEADLINE,
                interval=RESTAMP_POLL)
            if returned is None:
                return case.finish('inconclusive', 'the restarted '
                                   'writer never returned')
            case.observe('the restarted writer is serving as '
                         'active at tick '
                         + str(returned.get('tick')))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        if stream is not None:
            # Leave the rig as found: re-write the bare point's
            # baseline if the probe value still stands, then drop
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
