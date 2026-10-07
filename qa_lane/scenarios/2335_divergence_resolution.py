"""The divergence_resolution acceptance leg — one module per leg of
the scenario schedule; see qa_lane/scenarios/__init__.py for the
ordering rule and the shared seam."""
from .common import *

# Ordering: the leg settles the deployed pair, walks the divergence
# lifecycle end to end through the shared writer claim, and leaves
# the pair's launch roles, the field's stored values, and the
# declared fault census as found — the same restored window as the
# neighboring staged-output legs, before the legs that tune setpoints
# or force releases the field owner.
RUNS_AFTER = frozenset({'scenario_standby_divergence'})
RUNS_BEFORE = frozenset({'scenario_claim_reclaim'})


# --------------------------------------------------------------------
# The divergence lifecycle on the deployed pair — the per-revision
# lane evidence for WW-LCM-001's promotion-safety and audit clauses
# over the contract QA findings #541 and #542 landed (decision 26's
# positive-evidence clearing rule and its journaled resolution). The
# lifecycle has three legs and each is graded:
#
# - detection. A shared-claim attachment holds one field `Out` point
#   off the run's staged image — the `--owner-token` shape a third
#   attachment may join without preempting the owner — until the
#   tracking standby's same-tick comparison convicts it. The served
#   sync must read `diverged` naming the mismatched points with both
#   sides' values, a `divergence_detected` entry journals, and
#   `POST /promote` answers the named refusal. The shared claim never
#   fenced the active and never moved its role: the leg asserts the
#   active's ownership is undisturbed across the whole episode.
# - the blocked clear. An error fault injected on the compared field
#   points through the shipped tool's `fault` subcommand makes the
#   next applies' field reads fail, so `compare_staged_points` answers
#   an incomplete comparison: a failed read is neither divergence
#   evidence nor convergence evidence, so the `Diverged` verdict must
#   stand across those applies, no `divergence_resolved` may journal,
#   and the promote gate must stay closed. This is the #541
#   regression a false clear would reintroduce.
# - the journaled resolution. Clearing the fault restores a fully-read
#   comparison, and the first same-tick comparison that matches must
#   return the peer to `tracking`, journal exactly one
#   `divergence_resolved` carrying every compared point with both
#   sides' values, and reopen the promote gate.
#
# Named diagnostics: divergence-resolution-failed tags the contract
# clauses, divergence-resolution-nondeterministic the instability the
# contract does not answer for; two consecutive passes must produce
# identical evidence digests.

RESOLUTION_SETTLE = 45    # bound on the pair reporting settled
RESOLUTION_DEADLINE = 60  # bound on the comparison convicting
RESOLUTION_POLL = 0.05    # cadence of the poke-and-watch loop
RESOLUTION_BLOCKED = 20   # bound on the faulted applies' window
RESOLUTION_BLOCKED_ROUNDS = 8   # applies watched under the fault
RESOLUTION_RECOVER = 45   # bound on the clearing comparison resolving
RESOLUTION_PROMOTE = 30   # bound on a promotion settling
RESOLUTION_RESTORE = 45   # bound on the closing role restore
RESOLUTION_FAULT = 'disconnected'  # the error fault whose reads fail


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
    carries as a boolean. (None, None) when the deployed model
    declares no boolean field output."""
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
    """One field write under the standing shared claim."""
    return _plant_request(stream, {'op': 'write', 'point': point,
                                   'value': value})


def _hold_off_image(ctx, stream, point, value, tracked, deadline, watch):
    """Hold the field point off the run's staged image until the
    tracking standby's same-tick comparison convicts it. Returns
    `(report, poked, refused)`."""
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
        time.sleep(RESOLUTION_POLL)
    watch.append({'poked': poked, 'sync': None})
    return None, poked, None


def _await(ctx, name, match, bound, interval=RESOLUTION_POLL):
    """Poll one endpoint's served role report until `match` holds."""
    def hit():
        report = _try_role(ctx, ctx[name])
        return report if report is not None and match(report) else None
    return wait_for(hit, time.monotonic() + bound, interval=interval)


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
    role assignment back."""
    deadline = time.monotonic() + RESOLUTION_RESTORE
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
        time.sleep(RESOLUTION_POLL)
    return False


def _fault_points(ctx):
    """Every field `Out` point the plant serves — the staged image's
    whole surface, so an injected read fault leaves no compared point
    readable and the comparison cannot complete."""
    return sorted(_field_out_points(ctx))


def _clear(ctx, points):
    """Clear the injected read fault on each named point — the
    restoration the resolution leg depends on. Returns the first
    refusal, else None."""
    for point in points:
        answer = _try_plant_ctl(ctx, 'clear-fault', str(point))
        if answer is None:
            return 'the fault clear on point ' + str(point) \
                + ' was refused through the shipped tool'
    return None


def scenario_divergence_resolution(ctx):
    """Walk the divergence lifecycle on the deployed pair: detect the
    staged-versus-field skew, prove an incomplete comparison cannot
    clear it, and prove the first fully-read matching comparison
    resolves it with a journaled record and reopens the gate."""
    case = Case('divergence-resolution',
                'Diverged-standby detection, blocked clear, and '
                'journaled resolution',
                'with the deployed pair settled and tracking, a '
                'shared-claim field write under the settled active\'s '
                'pinned owner token flips the standby\'s served sync '
                'to diverged naming the mismatched points with both '
                'sides\' values, a divergence_detected entry journals, '
                'and POST /promote answers the named not_converged '
                'refusal while the shared claim never fenced or '
                'demoted the active: an error fault injected on the '
                'compared field points leaves the verdict standing '
                'across the applies whose field reads cannot complete '
                'with no divergence_resolved journaled and the gate '
                'still closed, and clearing the fault lets the first '
                'fully-read same-tick comparison return the peer to '
                'tracking with exactly one divergence_resolved '
                'carrying the compared points and both sides\' values '
                'and reopen the promote gate, leaving the pair\'s '
                'roles, the field, and the fault census as found')
    stream = None
    restore_point = None
    restore_value = None
    faulted = []
    promoted_name = None
    try:
        if ctx.get('plant') is None:
            return case.finish('inconclusive',
                               'the run publishes no plant endpoint')
        owner = wait_for(lambda: _pair_active(ctx),
                         time.monotonic() + RESOLUTION_SETTLE)
        if owner is None:
            return case.finish('failed',
                               'no deployed peer reports role=active')
        tracked = wait_for(lambda: _tracking_peer(ctx, owner),
                           time.monotonic() + RESOLUTION_SETTLE)
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
                'the plant serves — the divergence gate has no image '
                'to perturb')
        compared = _fault_points(ctx)
        if not compared:
            return case.finish(
                'inconclusive',
                'the plant serves no field output — the staged image '
                'covers nothing to compare')
        token = (ctx.get('plant_owner') or {}).get(owner)
        if token is None:
            return case.finish('inconclusive',
                               'the run pins no plant-writer owner '
                               'token for the settled active ' + owner)
        ref = save_evidence(ctx['evidence_dir'],
                            'divergence-resolution-image.json',
                            {'point': point, 'staged': staged,
                             'compared_points': compared,
                             'owner': owner, 'tracked': tracked})
        case.evidence('file', ref, 'the carried field output, the '
                      'staged value, and the compared point surface')

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

        # --- the detection leg ---------------------------------------
        injected = {'bool': not staged['bool']}
        watch = []
        report, poked, refused = _hold_off_image(
            ctx, stream, point, injected, tracked,
            time.monotonic() + RESOLUTION_DEADLINE, watch)
        ref = save_evidence(ctx['evidence_dir'],
                            'divergence-resolution-detection.json',
                            {'point': point, 'staged': staged,
                             'injected': injected, 'poked': poked,
                             'report': report, 'rounds': watch[-4:]})
        case.evidence('file', ref, 'the detection leg — the held '
                      'off-image field and the diverged report')
        if refused is not None:
            return case.finish(
                'failed',
                'divergence-resolution-nondeterministic: the '
                'shared-claim poke was refused after ' + str(poked)
                + ' writes: ' + json.dumps(refused)[:300])
        if report is None:
            return case.finish(
                'failed',
                'divergence-resolution-failed: the staged-versus-field '
                'comparison never convicted the standby — after '
                + str(poked) + ' shared-claim writes holding point '
                + str(point) + ' at ' + json.dumps(injected)
                + ', no served report read diverged')
        mismatches = _mismatches(report) or []
        named = [row for row in mismatches if row.get('point') == point]
        if len(named) != 1 or named[0].get('field') != injected \
                or named[0].get('staged') != staged:
            return case.finish(
                'failed',
                'divergence-resolution-failed: the diverged report '
                + json.dumps(mismatches)[:400] + ' does not name point '
                + str(point) + ' with both sides\' values — staged '
                + json.dumps(staged) + ' against field '
                + json.dumps(injected))
        detections = _journal_events(ctx[tracked], 'divergence_detected')
        durable = _durable_events((ctx.get('journal_files') or {})
                                  .get(tracked), 'divergence_detected')
        streamed = detections if detections else durable
        if streamed is None:
            return case.finish(
                'inconclusive',
                'neither the survivor\'s served journal nor its '
                'declared --journal-file could be read — the '
                'detection record has no durable surface to grade')
        if len(streamed) != 1:
            return case.finish(
                'failed',
                'divergence-resolution-failed: the survivor\'s journal '
                'carries ' + str(len(streamed)) + ' '
                'divergence_detected records — the transition journals '
                'once: ' + json.dumps(streamed)[:300])
        status, refused_body = _settle_call(ctx[tracked] + '/promote')
        if status != 409 or not (
                isinstance(refused_body, dict)
                and 'not_converged' in refused_body):
            return case.finish(
                'failed',
                'divergence-resolution-failed: POST /promote on the '
                'diverged peer answered ' + str(status) + ' '
                + json.dumps(refused_body)[:300] + ' — the gate must '
                'refuse with the named not_converged verdict')
        # --- the shared claim never disturbed the active --------------
        # The attachment joined the active's own claim; nothing about
        # the episode may fence it, demote it, or move its role.
        disturbed = _try_role(ctx, ctx[owner])
        if disturbed is None or disturbed.get('role') != 'active' \
                or disturbed.get('field_claim') != 'held':
            return case.finish(
                'failed',
                'divergence-resolution-failed: the shared-claim write '
                'disturbed the active\'s field ownership — it must keep '
                'its role and its claim across the whole episode: '
                + json.dumps(disturbed)[:300])

        ref = save_evidence(ctx['evidence_dir'],
                            'divergence-resolution-gate.json',
                            {'status': status, 'refused': refused_body,
                             'detections': detections, 'durable': durable})
        case.evidence('file', ref, 'the first promote gate and the '
                      'journaled detection')
        case.observe('the standby reported diverged naming point '
                     + str(point) + ' at tick '
                     + str(report.get('tick')) + ', the promote gate '
                       'refused not_converged')

        # --- the blocked-clear leg -----------------------------------
        # The read fault rides the shipped tool; the shared-claim
        # attachment keeps holding the point off the staged image, so
        # the only thing standing between the verdict and a match is
        # the comparison's own incompleteness.
        refused_fault = None
        for faulted_point in compared:
            if _try_plant_ctl(ctx, 'fault', str(faulted_point),
                              RESOLUTION_FAULT) is None:
                refused_fault = faulted_point
        faulted = list(compared)
        if refused_fault is not None:
            faulted = []
            return case.finish(
                'inconclusive',
                'the shipped tool refused the read fault on point '
                + str(refused_fault) + ' — the blocked-clear leg has no '
                'incomplete comparison to stage')
        blocked = []
        deadline = time.monotonic() + RESOLUTION_BLOCKED
        while time.monotonic() < deadline \
                and len(blocked) < RESOLUTION_BLOCKED_ROUNDS:
            try:
                _poke(stream, point, injected)
            except Exception:
                pass
            served = _try_role(ctx, ctx[tracked])
            blocked.append(None if served is None
                           else _sync_kind(served))
            time.sleep(RESOLUTION_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'divergence-resolution-blocked.json',
                            {'fault': RESOLUTION_FAULT,
                             'points': faulted, 'rounds': blocked})
        case.evidence('file', ref, 'the faulted applies\' served '
                      'verdicts — the blocked clear')
        answered = [row for row in blocked if row is not None]
        if len(answered) < 2:
            return case.finish(
                'inconclusive',
                'the survivor\'s monitor answered ' + str(len(answered))
                + ' reads across the faulted window — the blocked-clear '
                  'leg never observed its applies')
        if any(row != 'diverged' for row in answered):
            return case.finish(
                'failed',
                'divergence-resolution-failed: the survivor reported '
                + json.dumps(answered) + ' while the compared points\' '
                'field reads could not complete — an incomplete '
                'comparison is evidence of neither divergence nor '
                'convergence, so the diverged verdict must stand')
        mid = _journal_events(ctx[tracked], 'divergence_resolved')
        if mid:
            return case.finish(
                'failed',
                'divergence-resolution-failed: a divergence_resolved '
                'entry journaled ' + str(len(mid)) + ' times across the '
                'incomplete comparisons — the verdict must resolve only '
                'on a fully-read same-tick match: '
                + json.dumps(mid)[:300])
        status, refused_body = _settle_call(ctx[tracked] + '/promote')
        if status != 409 or not (
                isinstance(refused_body, dict)
                and 'not_converged' in refused_body):
            return case.finish(
                'failed',
                'divergence-resolution-failed: the promote gate reopened '
                'while the compared points\' reads could not complete — '
                'it answered ' + str(status) + ' '
                + json.dumps(refused_body)[:300])

        # --- the journaled-resolution leg ----------------------------
        problem = _clear(ctx, faulted)
        faulted = []
        if problem:
            return case.finish('failed',
                               'divergence-resolution-failed: '
                               + problem)
        restore_point, restore_value = point, staged
        verdict = _poke(stream, point, staged)
        if verdict.get('result') != 'done':
            return case.finish(
                'failed',
                'divergence-resolution-failed: the field restore write '
                'on point ' + str(point) + ' was refused: '
                + json.dumps(verdict)[:300])
        resolved = _await(ctx, tracked,
                          lambda served: _sync_kind(served) == 'tracking',
                          RESOLUTION_RECOVER)
        if resolved is None:
            return case.finish(
                'failed',
                'divergence-resolution-failed: the first fully-read '
                'same-tick comparison after the fault cleared never '
                'returned the peer to tracking — the verdict resolves '
                'only on positive evidence')
        resolutions = _journal_events(ctx[tracked], 'divergence_resolved')
        if resolutions is None or len(resolutions) != 1:
            return case.finish(
                'failed',
                'divergence-resolution-failed: the survivor\'s journal '
                'carries ' + str(len(resolutions or []))
                + ' divergence_resolved records — the resolution journals '
                  'once: ' + json.dumps(resolutions)[:300])
        compared_rows = (resolutions[0][1] or {}).get('compared') or []
        cleared = [row for row in compared_rows
                   if row.get('point') == point]
        if len(cleared) != 1 \
                or cleared[0].get('staged') != cleared[0].get('field') \
                or not compared_rows \
                or not all(row.get('staged') == row.get('field')
                           for row in compared_rows):
            return case.finish(
                'failed',
                'divergence-resolution-failed: the '
                'divergence_resolved record\'s compared evidence '
                + json.dumps(compared_rows)[:400] + ' — every compared '
                'point must be present with both sides\' values equal, '
                'point ' + str(point) + ' included')
        ref = save_evidence(ctx['evidence_dir'],
                            'divergence-resolution-resolution.json',
                            {'report': resolved,
                             'resolutions': resolutions,
                             'compared': len(compared_rows)})
        case.evidence('file', ref, 'the journaled resolution and the '
                      'compared points it names')
        case.observe('the first fully-read comparison resolved the '
                     'verdict at tick ' + str(resolutions[0][0])
                     + ' naming ' + str(len(compared_rows))
                     + ' compared points')

        # --- the second promote gate ---------------------------------
        status, body = _settle_call(ctx[tracked] + '/promote')
        if status != 200:
            return case.finish(
                'failed',
                'divergence-resolution-failed: promoting the resolved '
                'peer answered ' + str(status) + ' '
                + json.dumps(body)[:300] + ' — the resolution must '
                'reopen the promote gate')
        promoted_name = tracked
        promoted = _await(ctx, tracked,
                          lambda served: served.get('role') == 'active',
                          RESOLUTION_PROMOTE)
        if promoted is None or promoted.get('field_claim') != 'held':
            return case.finish(
                'failed',
                'divergence-resolution-failed: the promoted peer did not '
                'settle active holding the claim — its own claim must '
                'preempt the attachment\'s: ' + json.dumps(promoted)[:300])
        ref = save_evidence(ctx['evidence_dir'],
                            'divergence-resolution-promoted.json',
                            {'promoted': promoted})
        case.evidence('file', ref, 'the promoted peer holding the field')

        restore_point = None
        restore_value = None
        if not _restore_roles(ctx, tracked, owner):
            return case.finish(
                'failed',
                'divergence-resolution-failed: the closing demote/'
                'promote could not put the launch roles back — '
                + str(owner) + ' must own the field and ' + str(tracked)
                + ' track it again')
        promoted_name = None
        ref = save_evidence(ctx['evidence_dir'],
                            'divergence-resolution-restore.json',
                            {'owner': _role(ctx, ctx[owner]),
                             'peer': _try_role(ctx, ctx[tracked])})
        case.evidence('file', ref, 'the restored launch roles')
        case.observe('launch roles restored: ' + owner + ' active, '
                     + tracked + ' tracking')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        if ctx.get('plant_ctl') is not None and faulted:
            try:
                _clear(ctx, faulted)
            except Exception:
                pass
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