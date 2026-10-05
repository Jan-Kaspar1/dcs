"""The standby_divergence acceptance leg — one module per leg of
the scenario schedule; see qa_lane/scenarios/__init__.py for the
ordering rule and the shared seam."""
from .common import *

# Ordering: the leg settles the deployed pair, stages the
# staged-versus-field divergence through the shared writer claim, and
# leaves the pair's launch roles, the field's stored values, and the
# receipt log as found — so it takes the same restored window as the
# neighboring staged-output legs, and finishes before the legs that
# tune setpoints or force releases the field owner.
RUNS_AFTER = frozenset({'scenario_checkpoint_negotiation'})
RUNS_BEFORE = frozenset({'scenario_claim_reclaim'})


# --------------------------------------------------------------------
# The staged-versus-field divergence gate on the deployed pair — the
# per-revision lane evidence for WW-LCM-001's failover-integrity
# clause and WW-FND-002's pair semantics (decision 62's gate,
# decision 26's verdict). The gate is pinned in-platform by
# driven-scan tests; this leg grades it on the rig.
#
# The induction has to respect the field fence: once the active owns
# the claim, a plant-protocol `write` or `step` from any other
# attachment answers `Fenced`, and an injected error fault cannot
# produce divergence because `compare_staged_points` skips points
# whose field read fails. So the seam is the claim itself, and the
# only claim a third attachment may take without preempting the
# active is the *shared* one — the shape `--owner-token` exists for
# ("pins it when an external attachment must share the claim"): the
# attachment ensures the writer under the settled active's pinned
# token, the field answers `claimed_shared`, and the active keeps its
# ownership, its writes, and its `active` role throughout. The leg
# then holds one field `Out` point off the run's staged image with
# repeated pokes under that shared claim until the tracking standby's
# next same-tick comparison convicts it.
#
# The unconditional-preemption variant of the same seam is *not* what
# convicts: its preempting claim lands the owner's next write fenced,
# the owner demotes in place, and every later checkpoint stamps
# `source_owns_field: false` — which decision 87's orphan verdict
# supersedes a `Diverged` verdict outright and admits through the same
# promote. That sibling wedge is the unclaimed-rearm and claim-reclaim
# legs' subject; the staged-versus-field verdict this leg grades is
# the one a *shared* claim produces, because only then does the
# tracked line keep reporting that it owns the field.
#
# The leg then asserts the gate's whole surface and restores the rig:
# the diverged report names the mismatched point with both sides'
# values, `divergence_detected` journals once, `POST /promote` answers
# the named `not_converged` carrying that report with no field write
# resulting, the field restore (the reported staged value written back
# under the shared claim) returns the peer to `tracking` on fresh
# evidence rather than latching, the promoted peer's own claim
# preempts the attachment's and resumes field writes, and the closing
# demote/promote puts the pre-scenario role assignment back. The
# active's own ownership is undisturbed throughout — the shared claim
# never fenced it and never demoted it.
#
# Named diagnostics: standby-divergence-failed tags the contract
# clauses, standby-divergence-nondeterministic the instability the
# contract does not answer for; two consecutive passes must produce
# identical evidence digests.

DIVERGENCE_SETTLE = 45    # bound on the pair reporting settled
DIVERGENCE_DEADLINE = 60  # bound on the comparison convicting
DIVERGENCE_POLL = 0.05    # cadence of the poke-and-watch loop
DIVERGENCE_RECOVER = 45   # bound on the field restore reconverging
DIVERGENCE_PROMOTE = 30   # bound on a promotion settling
DIVERGENCE_RESTORE = 45   # bound on the closing role restore


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


def _mismatches(report):
    """The mismatch list a served `diverged` sync state carries, or
    None when the report is not diverged."""
    sync = (report or {}).get('sync')
    if isinstance(sync, dict) and isinstance(sync.get('diverged'), dict):
        return sync['diverged'].get('mismatches')
    return None


def _journal_events(base, kind):
    """The `(tick, payload)` stream one journal event kind carries on a
    served `GET /journal`, or None when the read dropped."""
    try:
        _, payload = http_json('GET', base + '/journal?since=0')
    except Exception:
        return None
    return [(entry.get('tick'), (entry.get('event') or {}).get(kind))
            for entry in _journal_list(payload)
            if isinstance((entry.get('event') or {}).get(kind), dict)]


def _durable_events(path, kind):
    """The same stream out of a declared `--journal-file`, or None where
    the file is not declared or cannot be read."""
    if not path or not Path(path).is_file():
        return None
    try:
        return [(item['entry'].get('tick'),
                 (item['entry'].get('event') or {}).get(kind))
                for item in _journal_entries(path)
                if 'entry' in item
                and isinstance((item['entry'].get('event')
                                or {}).get(kind), dict)]
    except Exception:
        return None


def _carried_out_point(ctx, base):
    """The field `Out` point the perturbation lands on: one the plant's
    own census serves as a field output and the owner's served image
    carries as a boolean, so the staged-versus-field comparison covers
    it. (None, None) when the deployed model declares no boolean field
    output — the leg then has nothing to perturb."""
    image = {entry.get('point'): entry.get('sample')
             for entry in (_snapshot(ctx, base).get('points') or [])
             if entry.get('direction') == 'out'}
    for point in _field_out_points(ctx):
        sample = image.get(point) or {}
        value = sample.get('value')
        if isinstance(value, dict) and isinstance(value.get('bool'), bool):
            return point, value
    return None, None


def _poke(stream, point, value):
    """One field write under the standing shared claim. Returns the
    plant's verdict object."""
    return _plant_request(stream, {'op': 'write', 'point': point,
                                   'value': value})


def _hold_off_image(ctx, stream, point, value, tracked, deadline, watch):
    """Hold the field point off the run's staged image under the shared
    claim until the tracking standby's next same-tick comparison
    convicts it. The active rewrites the point every scan, so a single
    write only opens a one-scan window; repeated pokes hold the field
    off-image across the comparison. Returns `(report, poked,
    refused)` — the refused verdict when a poke did not land."""
    poked = 0
    while time.monotonic() < deadline:
        verdict = _poke(stream, point, value)
        poked += 1
        if verdict.get('result') != 'done':
            watch.append({'poked': poked, 'verdict': verdict})
            return None, poked, verdict
        report = _try_role(ctx, ctx[tracked])
        if report is not None and _sync_kind(report) == 'diverged':
            watch.append({'poked': poked, 'sync': 'diverged'})
            return report, poked, None
        time.sleep(DIVERGENCE_POLL)
    watch.append({'poked': poked, 'sync': None})
    return None, poked, None


def _await(ctx, name, match, bound):
    """Poll one endpoint's served role report until `match` holds.
    Returns the matching report, else None."""
    def hit():
        report = _try_role(ctx, ctx[name])
        return report if report is not None and match(report) else None
    return wait_for(hit, time.monotonic() + bound,
                    interval=DIVERGENCE_POLL)


def _tracking(ctx, name):
    """The endpoint's report while it is a tracking standby, else
    None."""
    report = _try_role(ctx, ctx[name])
    if report is not None and report.get('role') == 'standby' \
            and _sync_kind(report) == 'tracking':
        return report
    return None


def _restore_roles(ctx, from_name, to_name):
    """The documented demote/promote order putting the pre-scenario
    role assignment back: the peer the leg promoted steps down and the
    original field owner takes the field back. Cleanup, never the
    contract the leg judges."""
    deadline = time.monotonic() + DIVERGENCE_RESTORE
    while time.monotonic() < deadline:
        try:
            if _pair_active(ctx) == to_name \
                    and _tracking(ctx, from_name) is not None:
                return True
            if _pair_active(ctx) == from_name:
                _settle_call(ctx[from_name] + '/demote')
            elif _tracking(ctx, to_name) is not None:
                _settle_call(ctx[to_name] + '/promote')
        except Exception:
            pass
        time.sleep(DIVERGENCE_POLL)
    return False


def scenario_standby_divergence(ctx):
    """Convict the deployed standby's staged image against the field
    and grade the promotion gate it closes: the diverged report names
    the point with both values, the refusal is the named
    `not_converged`, the field restore reconverges the peer, and the
    pair moves back to its launch roles."""
    case = Case('standby-divergence',
                'Standby divergence detection and the promotion gate',
                'with the deployed pair settled and tracking, a '
                'plant-protocol write under the settled active\'s '
                'shared writer claim holds one carried field output '
                'off the run\'s staged image until the standby\'s '
                'same-tick comparison convicts it: the standby\'s '
                'served report reads diverged naming the mismatched '
                'point with both sides\' values, divergence_detected '
                'journals once, POST /promote answers 409 '
                'not_converged carrying that report with no field '
                'write resulting and the active neither fenced nor '
                'demoted, writing the reported staged value back '
                'reconverges the peer to tracking rather than '
                'latching the verdict, the promoted peer\'s own claim '
                'preempts the attachment\'s and field writes resume, '
                'and demote/promote puts the launch roles back')
    stream = None
    restore_point = None
    restore_value = None
    promoted_name = None
    try:
        if ctx.get('plant') is None:
            return case.finish('inconclusive',
                               'the run publishes no plant endpoint')
        owner = wait_for(lambda: _pair_active(ctx),
                         time.monotonic() + DIVERGENCE_SETTLE)
        if owner is None:
            return case.finish('failed',
                               'no deployed peer reports role=active')
        peer = 'standby' if owner == 'active' else 'active'
        tracked = wait_for(lambda: _tracking_peer(ctx, owner),
                           time.monotonic() + DIVERGENCE_SETTLE)
        if tracked is None:
            return case.finish('inconclusive',
                               'no tracking peer — the deployed pair '
                               'never settled')
        base = ctx[owner]
        case.observe('settled pair: ' + owner + ' active, ' + tracked
                     + ' tracking')

        point, staged = _carried_out_point(ctx, base)
        if point is None:
            return case.finish(
                'inconclusive',
                'the deployed model declares no boolean field output '
                'the plant serves — the staged-versus-field gate has '
                'no image to perturb')
        tokens = ctx.get('plant_owner') or {}
        token = tokens.get(owner)
        if token is None:
            return case.finish('inconclusive',
                               'the run pins no plant-writer owner '
                               'token for the settled active ' + owner)
        ref = save_evidence(ctx['evidence_dir'],
                            'standby-divergence-image.json',
                            {'point': point, 'staged': staged,
                             'owner': owner, 'tracked': tracked,
                             'owner_token': token})
        case.evidence('file', ref, 'the carried field output and the '
                      'staged value the perturbation leaves behind')

        # The shared claim: the field answers `claimed_shared`, the
        # active keeps its ownership — the fence never turns.
        stream = _plant_connect(ctx)
        verdict = _plant_request(stream, {'op': 'ensure_writer',
                                          'owner': token})
        if verdict.get('result') not in ('done', 'claimed_shared'):
            return case.finish(
                'inconclusive',
                'the writer claim refused the shared attachment under '
                'the active\'s pinned token: ' + json.dumps(verdict)[:300])
        case.observe('plant protocol attached under ' + owner
                     + '\'s writer claim ('
                     + str(verdict.get('result')) + ')')

        injected = {'bool': not staged['bool']}
        watch = []
        report, poked, refused = _hold_off_image(
            ctx, stream, point, injected, tracked,
            time.monotonic() + DIVERGENCE_DEADLINE, watch)
        ref = save_evidence(ctx['evidence_dir'],
                            'standby-divergence-watch.json',
                            {'point': point, 'staged': staged,
                             'injected': injected, 'poked': poked,
                             'rounds': watch[-4:]})
        case.evidence('file', ref, 'the poke-and-watch loop that held '
                      'the field off the staged image')
        if refused is not None:
            return case.finish(
                'failed',
                'standby-divergence-nondeterministic: the shared-claim '
                'poke was refused after ' + str(poked) + ' writes: '
                + json.dumps(refused)[:300])
        if report is None:
            return case.finish(
                'failed',
                'standby-divergence-failed: the staged-versus-field '
                'comparison never convicted the standby — after '
                + str(poked) + ' shared-claim writes holding point '
                + str(point) + ' at ' + json.dumps(injected)
                + ' against the staged ' + json.dumps(staged)
                + ', no served report read diverged: the standby must '
                  'report the named verdict, never healthy tracking')
        mismatches = _mismatches(report) or []
        named = [row for row in mismatches if row.get('point') == point]
        if len(named) != 1 \
                or named[0].get('staged') != staged \
                or named[0].get('field') != injected:
            return case.finish(
                'failed',
                'standby-divergence-failed: the diverged report '
                + json.dumps(mismatches)[:400] + ' does not name point '
                + str(point) + ' with both sides\' values (staged '
                + json.dumps(staged) + ', field '
                + json.dumps(injected) + ') — the gate\'s evidence '
                  'must name what it compares')
        case.observe('the standby reported diverged naming point '
                     + str(point) + ' staged ' + json.dumps(staged)
                     + ' against field ' + json.dumps(injected)
                     + ' at tick ' + str(report.get('tick')))

        # The journaled detection, served and durable.
        detections = _journal_events(ctx[tracked], 'divergence_detected')
        durable = _durable_events((ctx.get('journal_files') or {})
                                  .get(tracked),
                                  'divergence_detected')
        streamed = detections if detections else durable
        if detections is None and durable is None:
            return case.finish(
                'inconclusive',
                'neither the survivor\'s served journal nor its '
                'declared --journal-file could be read — the '
                'detection record has no durable surface to grade')
        if len(streamed or []) != 1:
            return case.finish(
                'failed',
                'standby-divergence-failed: the survivor\'s journal '
                'carries ' + str(len(streamed or []))
                + ' divergence_detected records — the transition into '
                  'the diverged verdict journals exactly once: '
                + json.dumps(streamed)[:300])
        ref = save_evidence(ctx['evidence_dir'],
                            'standby-divergence-detection.json',
                            {'served': detections, 'durable': durable,
                             'tracked': tracked})
        case.evidence('file', ref, 'the journaled divergence_detected '
                      'entry on the compared tick')

        # The gate: the refusal carries the report, and nothing moved.
        status, refused = _settle_call(ctx[tracked] + '/promote')
        refusal_sync = None
        if isinstance(refused, dict) \
                and isinstance(refused.get('not_converged'), dict):
            refusal_sync = refused['not_converged'].get('sync')
        if status != 409 or refusal_sync is None:
            return case.finish(
                'failed',
                'standby-divergence-failed: POST /promote on the '
                'diverged peer answered ' + str(status) + ' '
                + json.dumps(refused)[:300] + ' — the gate must refuse '
                'with the named not_converged verdict, never admit')
        if _mismatches({'sync': refusal_sync}) != mismatches:
            return case.finish(
                'failed',
                'standby-divergence-failed: the refusal carries '
                + json.dumps(refusal_sync)[:300] + ' — the answer must '
                'carry the diverged report it declined on')
        after = _try_role(ctx, ctx[tracked])
        if after is not None and after.get('role') != 'standby':
            return case.finish(
                'failed',
                'standby-divergence-failed: the refused promotion moved '
                'the peer to ' + str(after.get('role')) + ' — the gate '
                'must hand off no field')
        owner_after = _try_role(ctx, ctx[owner])
        if owner_after is None or owner_after.get('role') != 'active' \
                or owner_after.get('field_claim') != 'held':
            return case.finish(
                'failed',
                'standby-divergence-failed: the shared claim disturbed '
                'the active — it must keep its role and its claim: '
                + json.dumps(owner_after)[:300])
        ref = save_evidence(ctx['evidence_dir'],
                            'standby-divergence-refusal.json',
                            {'status': status, 'refused': refused,
                             'survivor': after, 'owner': owner_after})
        case.evidence('file', ref, 'the refused promotion carrying the '
                      'diverged report, with the active undisturbed')

        # The field restore: the reported staged value written back,
        # and the peer's next clean comparison releases the gate on
        # fresh evidence rather than latching it.
        restore_point, restore_value = point, staged
        verdict = _poke(stream, point, staged)
        if verdict.get('result') != 'done':
            return case.finish(
                'failed',
                'standby-divergence-failed: the field restore write on '
                'point ' + str(point) + ' was refused under the shared '
                'claim: ' + json.dumps(verdict)[:300])
        converged = _await(ctx, tracked,
                           lambda served: _sync_kind(served) == 'tracking',
                           DIVERGENCE_RECOVER)
        if converged is None:
            return case.finish(
                'failed',
                'standby-divergence-failed: the field restore never '
                'reconverged the peer — point ' + str(point)
                + ' back at the reported staged ' + json.dumps(staged)
                + ' must clear the verdict on the next fully-read '
                  'same-tick comparison')
        resolutions = _journal_events(ctx[tracked], 'divergence_resolved')
        ref = save_evidence(ctx['evidence_dir'],
                            'standby-divergence-resolution.json',
                            {'report': converged,
                             'resolutions': resolutions})
        case.evidence('file', ref, 'the reconverged verdict and its '
                      'journaled resolution')

        # The closing promotion: the promoted peer's own claim
        # preempts the attachment's and field writes resume.
        status, body = _settle_call(ctx[tracked] + '/promote')
        if status != 200:
            return case.finish(
                'failed',
                'standby-divergence-failed: promoting the reconverged '
                'peer answered ' + str(status) + ' '
                + json.dumps(body)[:300] + ' — a fresh comparison must '
                'reopen the gate')
        promoted_name = tracked
        promoted = _await(ctx, tracked,
                          lambda served: served.get('role') == 'active',
                          DIVERGENCE_PROMOTE)
        if promoted is None or promoted.get('field_claim') != 'held':
            return case.finish(
                'failed',
                'standby-divergence-failed: the promoted peer did not '
                'settle active holding the claim — its own claim must '
                'preempt the attachment\'s: ' + json.dumps(promoted)[:300])
        if _pair_active(ctx) != tracked:
            return case.finish(
                'failed',
                'standby-divergence-failed: the promotion left '
                + str(_pair_active(ctx)) + ' owning the field — exactly '
                'one peer must own it')
        case.observe('the promoted peer took the field under its own '
                     'claim and resumed field writes')
        restore_point = None
        restore_value = None
        ref = save_evidence(ctx['evidence_dir'],
                            'standby-divergence-promoted.json',
                            {'promoted': promoted, 'owner': tracked,
                             'demoted': peer})
        case.evidence('file', ref, 'the promoted peer holding the field')

        # The pair's launch roles move back.
        if not _restore_roles(ctx, tracked, owner):
            return case.finish(
                'failed',
                'standby-divergence-failed: the closing demote/promote '
                'could not put the launch roles back — ' + str(owner)
                + ' must own the field and ' + str(tracked)
                + ' track it again')
        promoted_name = None
        ref = save_evidence(ctx['evidence_dir'],
                            'standby-divergence-restore.json',
                            {'owner': _role(ctx, ctx[owner]),
                             'peer': _try_role(ctx, ctx[tracked])})
        case.evidence('file', ref, 'the restored launch roles')
        case.observe('launch roles restored: ' + owner + ' active, '
                     + tracked + ' tracking')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        # The field's stored values are the run's shared state: a case
        # that leaves the perturbed point standing poisons every later
        # scenario, and a case that promoted a peer puts the launch
        # roles back.
        if stream is not None:
            if restore_point is not None:
                try:
                    _poke(stream, restore_point, restore_value)
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
        if promoted_name is not None:
            try:
                _restore_roles(ctx, promoted_name,
                               'standby' if promoted_name == 'active'
                               else 'active')
            except Exception:
                pass