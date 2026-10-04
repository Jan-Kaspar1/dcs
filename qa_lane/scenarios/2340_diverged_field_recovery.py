"""The diverged_field_recovery acceptance leg — one module per leg of
the scenario schedule; see qa_lane/scenarios/__init__.py for the
ordering rule and the shared seam."""
from .common import *

# Ordering: the leg settles the deployed pair, wedges the field, and
# recovers it by relaunching the wedged field owner as a fresh active
# — so it takes the same restored window as the neighboring
# staged-output legs, after the divergence-lifecycle leg that owns the
# same shared-claim seam, and finishes before the legs that tune
# setpoints or force releases the field owner.
RUNS_AFTER = frozenset({'scenario_divergence_resolution'})
RUNS_BEFORE = frozenset({'scenario_claim_reclaim'})


# --------------------------------------------------------------------
# The diverged-field wedge and its restart-as-active recovery on the
# deployed pair — the per-revision lane evidence for decision 94's
# recorded remedy (WW-LCM-001 continuity, WW-OPS-003 field
# confidence) and decision 90's unclaimed-field surface (#901). The
# wedge is QA finding `diverged-field-wedge-no-controller-recovery`: a
# protocol-legal `claim_writer` preempt, one field `Out` write, and
# `release_writer` leave the field unclaimed while it holds an
# actuation value no run ever commanded — reachable non-maliciously
# as a maintenance write left unrestored. Every promote is then refused
# `not_converged`, and the two fail-closed rules composing into a dead
# end is exactly the self-sealing shape decision 94 answers with the
# operator relaunching one controller as a fresh active: the
# conditional startup grant takes the free field, the restartee's
# declared image rewrites the un-commanded value, and the diverged
# peer's next same-tick comparison clears to tracking in place with a
# journaled `divergence_resolved`.
#
# Three properties of the current contract shape the run, and each is
# asserted rather than assumed:
#
# - the comparison's window. Decision 26's staged-versus-field verdict
#   convicts only while the tracked line's source still stamps
#   `source_owns_field: true`, and the fenced owner's demotion is the
#   checkpoint that flips that stamp; decision 87's `orphaned` verdict
#   then supersedes `Diverged` outright. The leg therefore grades the
#   named verdict the contract serves and, when that verdict is
#   `diverged`, requires it to name the skewed point with both sides'
#   values — the evidence #730's reproduction read.
# - the demoted ex-owner's claim. Decision 97's fencing-loss reclaim
#   takes a free field back on its next scan, so the unclaimed-field
#   surface is observable only with that actor gone. The leg removes
#   it the way the recorded remedy presupposes — `stop_controller` on
#   the wedged field owner, the remedy's first half — and the
#   `start_controller` on the same seat is its second.
# - the un-commanded value. The hazard is the standing actuation, not
#   the role, so the leg reads the skewed point off the plant's own
#   census before and after and requires the declared image to
#   overwrite it.
#
# Named diagnostics: wedge-recovery-failed tags the contract clauses,
# wedge-recovery-nondeterministic the instability the contract does
# not answer for; two consecutive passes must produce identical
# evidence digests.

WEDGE_SETTLE = 45    # bound on the pair reporting settled
WEDGE_DEADLINE = 60  # bound on the wedge's served verdicts landing
WEDGE_POLL = 0.5     # cadence watching the wedge's served surface
WEDGE_RECOVER = 60   # bound on the relaunched owner settling active
WEDGE_HEAL = 90      # bound on the survivor's reconvergence
WEDGE_RESTORE = 60   # bound on the launch-role restore
# The interposer's claim token — a different owner than either peer
# pins, so the unconditional `claim_writer` preempts the field
# owner's standing claim the finding's reproduction recorded.
WEDGE_FOREIGN = 0x7765_6765_2d72_6367  # "qwedge-rcg"
# The sync verdicts a wedged pair serves: every one of them closes the
# ordinary promote gate, and each names the peer that reported it.
WEDGE_UNCONVERGED = ('diverged', 'orphaned', 'usurped', 'degraded',
                     'unsynchronized')


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


def _role_events(base, kind):
    """The `role_changed` payloads a served journal carries."""
    try:
        _, payload = http_json('GET', base + '/journal?since=0')
    except Exception:
        return None
    return [(entry.get('event') or {}).get(kind)
            for entry in _journal_list(payload)
            if isinstance((entry.get('event') or {}).get(kind), dict)]


def _pair_faults(ctx, reports):
    """The page's pairHealth verdict over the served reports — the
    surface the deploy shows. An unreachable peer is named, the field
    standing unclaimed is the named `field_unclaimed` fault, and
    exactly one active peer with no fault is the only healthy shape."""
    faults, actives = [], []
    for name, report in sorted(reports.items()):
        if report is None:
            faults.append(name + ' unreachable')
            continue
        if report.get('role') == 'active':
            actives.append(name)
        elif _sync_kind(report) in ('diverged', 'degraded'):
            faults.append(name + ' ' + _sync_kind(report))
        if report.get('field_claim') == 'unclaimed':
            faults.append('field_unclaimed')
    return faults, actives


def _carried_out_point(ctx, base):
    """The field `Out` point the wedge's un-commanded write lands on:
    one the plant's own census serves as a field output and the
    owner's served image carries as a boolean. (None, None) when the
    deployed model declares no boolean field output."""
    image = {entry.get('point'): entry.get('sample')
             for entry in (_snapshot(ctx, base).get('points') or [])
             if entry.get('direction') == 'out'}
    for point in _field_out_points(ctx):
        sample = image.get(point) or {}
        value = sample.get('value')
        if isinstance(value, dict) and isinstance(value.get('bool'), bool):
            return point, value
    return None, None


def _field_value(ctx, point):
    """The plant's stored sample for `point`, or None when the read
    dropped — a lost observation, never the leg's verdict."""
    sample = _field_sample(ctx, point)
    return (sample or {}).get('value')


def _await(ctx, names, match, bound, interval=WEDGE_POLL):
    """Poll the named endpoints' served role reports until every one
    that answers satisfies `match` and none is None. Returns the
    reports, else None."""
    def hit():
        reports = {name: _try_role(ctx, ctx[name]) for name in names}
        if any(report is None for report in reports.values()):
            return None
        return reports if all(match(report) for report in
                              reports.values()) else None
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
    deadline = time.monotonic() + WEDGE_RESTORE
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
        time.sleep(WEDGE_POLL)
    return False


def scenario_diverged_field_recovery(ctx):
    """Wedge the field unclaimed and un-commanded, read the served
    surface it leaves, and prove the recorded restart-as-active
    remedy heals the pair."""
    case = Case('diverged-field-recovery',
                'Diverged-field wedge and its restart-as-active '
                'recovery',
                'with the deployed pair settled and tracking, a '
                'plant-protocol claim_writer preempt, one field Out '
                'write off the staged value, and release_writer leave '
                'the field unclaimed holding an un-commanded '
                'actuation: the fenced field owner demotes in place '
                'with its field_claim_lost journaled naming the '
                'interposer, the survivor serves a named un-converged '
                'verdict — naming the skewed point with both sides\' '
                'values wherever it reads diverged — and refuses '
                'POST /promote not_converged, the served report carries '
                'field_claim unclaimed with the pair-health view '
                'naming the field_unclaimed fault while the skewed '
                'point still stores the un-commanded value; the '
                'recorded remedy then relaunches the wedged field '
                'owner as a fresh active whose conditional startup '
                'grant takes the free field, its declared image '
                'overwrites the un-commanded value, exactly one peer '
                'reports active, and the survivor reconverges to '
                'tracking in place before the launch roles restore')
    stream = None
    restore_point = None
    restore_value = None
    stopped = None
    try:
        if ctx.get('plant') is None:
            return case.finish('inconclusive',
                               'the run publishes no plant endpoint')
        owner = wait_for(lambda: _pair_active(ctx),
                         time.monotonic() + WEDGE_SETTLE)
        if owner is None:
            return case.finish('failed',
                               'no deployed peer reports role=active')
        survivor = 'standby' if owner == 'active' else 'active'
        tracked = wait_for(lambda: _tracking_peer(ctx, owner),
                           time.monotonic() + WEDGE_SETTLE)
        if tracked is None:
            return case.finish('inconclusive',
                               'no tracking peer — the deployed pair '
                               'never settled')
        if tracked != survivor:
            return case.finish('inconclusive',
                               'the deployed pair serves more than one '
                               'tracking standby — the wedge is scoped '
                               'to the pair the launch roles name')
        stop = ctx.get('stop_controller')
        start = ctx.get('start_controller')
        if stop is None or start is None:
            return case.finish('inconclusive',
                               'the run context carries no '
                               'stop_controller/start_controller seam — '
                               'the recorded remedy is an operator '
                               'relaunch')
        base = ctx[owner]
        case.observe('settled pair: ' + owner + ' active, ' + survivor
                     + ' tracking')

        point, staged = _carried_out_point(ctx, base)
        if point is None:
            return case.finish(
                'inconclusive',
                'the deployed model declares no boolean field output '
                'the plant serves — the wedge has no image to skew')
        tokens = ctx.get('plant_owner') or {}
        ref = save_evidence(ctx['evidence_dir'],
                            'wedge-recovery-image.json',
                            {'point': point, 'staged': staged,
                             'owner': owner, 'survivor': survivor,
                             'owner_token': tokens.get(owner),
                             'interposer_token': WEDGE_FOREIGN})
        case.evidence('file', ref, 'the carried field output, the '
                      'staged value, and the interposer\'s claim token')

        # --- the wedge: preempt, one write, release ------------------
        stream = _plant_connect(ctx)
        claim = _plant_request(stream, {'op': 'claim_writer',
                                        'owner': WEDGE_FOREIGN})
        if claim.get('result') not in ('done', 'claimed_shared'):
            return case.finish(
                'inconclusive',
                'the interposer\'s claim_writer answered '
                + json.dumps(claim)[:300] + ' — the wedge the leg '
                'stages never stood')
        injected = {'bool': not staged['bool']}
        write = _plant_request(stream, {'op': 'write', 'point': point,
                                        'value': injected})
        if write.get('result') != 'done':
            return case.finish(
                'failed',
                'wedge-recovery-failed: the interposer\'s write on point '
                + str(point) + ' answered ' + json.dumps(write)[:300]
                + ' — the un-commanded actuation the wedge leaves '
                  'standing never landed')
        landed = (_plant_read(ctx, point).get('value') or {})
        if landed != injected:
            return case.finish(
                'failed',
                'wedge-recovery-failed: the interposer\'s write left '
                'point ' + str(point) + ' at ' + json.dumps(landed)
                + ' — the field never held the un-commanded value')
        case.observe('the interposer preempted the field and wrote '
                     'point ' + str(point) + ' = '
                     + json.dumps(injected) + ' off the staged '
                     + json.dumps(staged))

        # The fenced owner's first field write is superseded, and it
        # demotes in place: the journaled loss and the fenced-origin
        # walk are the fence contract's own record.
        demoted = _await(ctx, [owner],
                         lambda report: report.get('role') == 'standby',
                         WEDGE_DEADLINE)
        if demoted is None:
            return case.finish(
                'failed',
                'wedge-recovery-failed: the preempted field owner never '
                'settled standby — its first fenced write must demote it '
                'in place')
        losses = _role_events(ctx[owner], 'field_claim_lost')
        walk = _role_events(ctx[owner], 'role_changed') or []
        fenced_walk = any(
            change.get('to') in ('demoting', 'standby')
            and change.get('origin') == 'fenced' for change in walk)
        durable_losses = _durable_events(
            (ctx.get('journal_files') or {}).get(owner),
            'field_claim_lost')
        streamed = losses if losses is not None else durable_losses
        if streamed is None:
            return case.finish(
                'inconclusive',
                'neither the field owner\'s served journal nor its '
                'declared --journal-file could be read — the fencing '
                'loss has no record to grade')
        if len(streamed) != 1:
            return case.finish(
                'failed',
                'wedge-recovery-failed: the field owner\'s journal '
                'carries ' + str(len(streamed)) + ' field_claim_lost '
                'records — one held claim loses it once')
        claimant = (streamed[0][1] if streamed and isinstance(
            streamed[0], list) else streamed[0]) or {}
        if isinstance(claimant, dict) \
                and claimant.get('claimant') not in (None, WEDGE_FOREIGN):
            return case.finish(
                'failed',
                'wedge-recovery-failed: the field_claim_lost record '
                'names claimant ' + str(claimant.get('claimant'))
                + ', not the interposer\'s token ' + str(WEDGE_FOREIGN)
                + ' — the fencing verdict must attribute the preemption')
        if not fenced_walk:
            return case.finish(
                'failed',
                'wedge-recovery-failed: the field owner\'s journal '
                'carries the role walk ' + json.dumps(walk)[:300]
                + ' — no fenced-origin demotion was journaled')
        ref = save_evidence(ctx['evidence_dir'],
                            'wedge-recovery-fenced.json',
                            {'owner': owner, 'losses': streamed,
                             'durable_losses': durable_losses,
                             'fenced_walk': fenced_walk})
        case.evidence('file', ref, 'the journaled field_claim_lost and '
                      'the fenced-origin demotion')

        # --- the wedge's served verdict on the survivor --------------
        served = _await(ctx, [survivor],
                        lambda report: _sync_kind(report)
                        in WEDGE_UNCONVERGED,
                        WEDGE_DEADLINE)
        if served is None:
            return case.finish(
                'failed',
                'wedge-recovery-nondeterministic: the surviving peer '
                'never served a named un-converged verdict — the '
                'comparison the skewed field must convict never landed')
        verdict = _sync_kind(served[survivor])
        mismatches = _mismatches(served[survivor]) or []
        named = [row for row in mismatches if row.get('point') == point]
        if verdict == 'diverged' and (len(named) != 1
                                      or named[0].get('field') != injected):
            return case.finish(
                'failed',
                'wedge-recovery-failed: the diverged report '
                + json.dumps(mismatches)[:400] + ' does not name point '
                + str(point) + ' with both sides\' values against '
                'field ' + json.dumps(injected))
        case.observe('the survivor reported ' + verdict
                     + ' at tick ' + str(served[survivor].get('tick')))

        # --- the recorded remedy's first half: remove the wedged ----
        # field owner. Decision 97's fencing-loss reclaim would take a
        # free field back on its next scan, so the unclaimed-field
        # surface is observable only with that actor gone — and
        # decision 94's remedy is a *fresh* authority in its place.
        stop(owner)
        stopped = owner
        release = _plant_request(stream, {'op': 'release_writer'})
        if release.get('result') != 'done':
            return case.finish(
                'failed',
                'wedge-recovery-failed: the interposer\'s release_writer '
                'answered ' + json.dumps(release)[:300])

        # --- the unclaimed surface on what now answers --------------
        wedged = {survivor: _try_role(ctx, ctx[survivor])}
        if wedged[survivor] is None:
            return case.finish(
                'inconclusive',
                'the surviving peer stopped answering while the wedge '
                'stood — the unclaimed surface was never served')
        faults, actives = _pair_faults(ctx, wedged)
        probe = _plant_probe(ctx, {'op': 'write', 'point': point,
                                   'value': injected})
        field_kind = (probe.get('error') or {}).get('kind')
        stored = _field_value(ctx, point)
        if 'field_unclaimed' not in faults:
            return case.finish(
                'failed',
                'wedge-recovery-failed: the pair-health verdict names '
                + json.dumps(faults) + ' — no field_unclaimed fault on '
                'the released field')
        if field_kind != 'unclaimed':
            return case.finish(
                'failed',
                'wedge-recovery-failed: the released field\'s mutation '
                'probe answered ' + json.dumps(probe)[:300] + ' — an '
                'unclaimed field must refuse every mutation')
        if stored != injected:
            return case.finish(
                'failed',
                'wedge-recovery-failed: the released field stores '
                + json.dumps(stored) + ' on point ' + str(point)
                + ' — the un-commanded actuation must still stand at '
                + json.dumps(injected))
        # The promotion gate, graded on the verdict the contract serves:
        # a `diverged` peer is refused `not_converged` with the report
        # attached, and no override admits it.
        status = None
        refused = None
        if verdict == 'diverged':
            status, refused = _settle_call(ctx[survivor] + '/promote')
            if status != 409 or not (isinstance(refused, dict)
                                     and 'not_converged' in refused):
                return case.finish(
                    'failed',
                    'wedge-recovery-failed: POST /promote on the diverged '
                    'survivor answered ' + str(status) + ' '
                    + json.dumps(refused)[:300] + ' — the gate must '
                    'refuse with the named not_converged verdict and '
                    'carry no override')
        ref = save_evidence(ctx['evidence_dir'],
                            'wedge-recovery-unclaimed.json',
                            {'verdict': verdict, 'report': wedged,
                             'faults': faults, 'actives': actives,
                             'probe': probe, 'stored': stored,
                             'promote': [status, refused]})
        case.evidence('file', ref, 'the unclaimed field surface — the '
                      'served claim, the pair-health fault, the '
                      'fail-closed probe, and the stored value')
        case.observe('the released field is unclaimed and un-commanded '
                     'on point ' + str(point) + ' ('
                     + json.dumps(stored) + '), the pair view naming '
                     'field_unclaimed')

        # --- the remedy's second half: relaunch as a fresh active ---
        restore_point, restore_value = point, staged
        start(owner)
        stopped = None
        relaunched = _await(ctx, [owner],
                            lambda report: report.get('role') == 'active'
                            and report.get('field_claim') == 'held',
                            WEDGE_RECOVER)
        if relaunched is None:
            return case.finish(
                'failed',
                'wedge-recovery-failed: the relaunched field owner never '
                'settled active holding the claim — the conditional '
                'startup grant must take the free field')
        ref = save_evidence(ctx['evidence_dir'],
                            'wedge-recovery-relaunch.json',
                            {'owner': relaunched})
        case.evidence('file', ref, 'the relaunched owner active with '
                      'the claim held')
        case.observe('the relaunched controller took the free field '
                     'through its conditional startup grant')

        # The declared image overwrites the un-commanded value.
        healed = _await(ctx, [owner],
                        lambda report: _field_value(ctx, point)
                        == staged,
                        WEDGE_HEAL)
        if healed is None:
            return case.finish(
                'failed',
                'wedge-recovery-failed: the relaunched owner\'s declared '
                'image never overwrote the un-commanded value on point '
                + str(point) + ' — it still stores '
                + json.dumps(_field_value(ctx, point)))
        if _pair_active(ctx) != owner:
            return case.finish(
                'failed',
                'wedge-recovery-failed: the recovery left '
                + str(_pair_active(ctx)) + ' owning the field — exactly '
                'one peer must own it')
        restore_point = None
        restore_value = None

        # --- the survivor reconverges in place ----------------------
        converged = _await(ctx, [survivor],
                           lambda report: _sync_kind(report) == 'tracking',
                           WEDGE_HEAL)
        if converged is None:
            return case.finish(
                'failed',
                'wedge-recovery-failed: the survivor never reconverged to '
                'tracking on the relaunched owner\'s stream — the '
                'recovery must heal the pair in place')
        resolutions = _journal_events(ctx[survivor], 'divergence_resolved')
        detections = _journal_events(ctx[survivor], 'divergence_detected')
        ref = save_evidence(ctx['evidence_dir'],
                            'wedge-recovery-reconverged.json',
                            {'report': converged[survivor],
                             'detections': detections,
                             'resolutions': resolutions})
        case.evidence('file', ref, 'the survivor\'s reconvergence and '
                      'its divergence records')
        case.observe('the survivor reconverged to tracking at tick '
                     + str(converged[survivor].get('tick')))

        # --- the launch roles restore -------------------------------
        final = {owner: _try_role(ctx, ctx[owner]),
                 survivor: _try_role(ctx, ctx[survivor])}
        faults, actives = _pair_faults(ctx, final)
        if faults or actives != [owner]:
            return case.finish(
                'failed',
                'wedge-recovery-failed: the pair did not restore to a '
                'settled launch shape — faults ' + json.dumps(faults)
                + ', actives ' + json.dumps(actives))
        ref = save_evidence(ctx['evidence_dir'],
                            'wedge-recovery-restore.json',
                            {'reports': final, 'faults': faults,
                             'stored': _field_value(ctx, point)})
        case.evidence('file', ref, 'the restored launch roles and the '
                      'field\'s restored stored value')
        case.observe('launch roles restored: ' + owner + ' active, '
                     + survivor + ' tracking')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        # The operator's relaunch and the field's stored values are the
        # run's shared state: a case that leaves the wedged owner down
        # or the skewed point standing poisons every later scenario.
        if stopped is not None:
            try:
                ctx['start_controller'](stopped)
            except Exception:
                pass
        if stream is not None:
            # Only ever restore a field the wedge left ownerless: a
            # live claim belongs to whoever holds it, and preempting it
            # here would leave the pair worse than the leg found it.
            if restore_point is not None \
                    and _probe_error(_try_plant(
                        ctx, {'op': 'probe_writer'})) == 'unclaimed':
                try:
                    _plant_request(stream, {'op': 'write',
                                           'point': restore_point,
                                           'value': restore_value})
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