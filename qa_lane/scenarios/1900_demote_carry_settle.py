"""The demote_carry_settle acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *


# --------------------------------------------------------------------
# The demote-carry boundary's single-outcome contract — the #829 fix's
# rig evidence for WW-FND-004's receipted-command clause and
# WW-LCM-001's audit clause, pinned distinctly from the
# demote-settle-uniqueness leg's broad race: where that leg demotes
# the field owner deliberately, this leg drives the involuntary
# boundary the finding reproduces — a rogue claim_writer preempts the
# owner's field claim while receipted submissions stand pending, the
# superseded run's detection scan demotes it in place, and the
# tracking peer's pulls carry the suspended admissions Accepted
# across the boundary until its promote applies them on the live
# line. The #829 contract: the detection boundary's provisional
# settlements re-suspend — the surviving line adjudicates each
# admission once — where the pre-fix run minted Rejected(Superseded)
# at the demote and journaled again when the carried copy applied:
# two terminal outcomes for one admission. The audit reuses the
# settle leg's evidence clauses under this leg's own diagnostics:
# exactly one terminal outcome per raced admission — applied at a
# scan boundary or the named superseded rejection, never the pair —
# both peers' adopted receipt logs carrying that same single outcome,
# the served image reflecting at most one application, named
# diagnostics on failure, and two passes producing identical
# digests.

DEMOTE_CARRY_SETTLE = 45    # bound on the demote, the carry, and each
                          # role settle inside the leg
DEMOTE_CARRY_AUDIT = 30     # bound on an admission's terminal
                          # journaled outcome
DEMOTE_CARRY_POLL = 0.4     # cadence on the carry and settle waits
DEMOTE_CARRY_WATCH = 0.05   # cadence polling the superseded peer
                          # mid-demotion — the demoting walk is one scan
DEMOTE_CARRY_ADMISSIONS = 3  # receipted submissions raced pre-claim
DEMOTE_CARRY_POST = 2        # submissions fired into the fenced window


def _following_standby(ctx, name):
    """The endpoint's report while it holds a promotable standby
    posture — tracking, orphaned, or reinitialized, the converged
    shapes `POST /promote` accepts — else None."""
    try:
        report = _role(ctx, ctx[name])
    except Exception:
        return None
    if report.get('role') == 'standby' \
            and set(report.get('sync') or {}) \
            & {'tracking', 'orphaned', 'reinitialized'}:
        return report
    return None


def _receipt_at(receipts, base, index):
    """The served log's receipt at absolute submission `index`, or
    None when the retained window does not reach it."""
    position = index - base
    if 0 <= position < len(receipts):
        return receipts[position]
    return None


def _carry_window(ctx, peer_base, admissions):
    """The tracking peer's adopted receipt window once it covers the
    raced admissions — every admission's receipt present, and at
    least one still reading `accepted`: the suspended log the
    tracking pulls carried live across the demote, awaiting the
    successor's verdict rather than a provisional one. None while any
    admission has not landed in the peer's window — a read the next
    pull may still complete, never the leg's verdict."""
    try:
        receipts, base = _receipt_window(ctx, peer_base)
    except Exception:
        return None
    covered = []
    for admission in admissions:
        receipt = _receipt_at(receipts, base, admission['index'])
        if receipt is None:
            return None
        covered.append(receipt)
    if not any(_outcome_key(receipt) == 'accepted'
               for receipt in covered):
        return None
    return {'receipts': covered}


def _demote_carry_pass(ctx, number, entry_owner, points):
    """One demote-carry pass: race receipted writable-point
    submissions on the field owner, preempt its claim with a rogue
    writer so the fenced detection scan demotes it in place, let the
    tracking peer's pulls carry the suspended admissions across, and
    promote it to apply them — then the per-admission audit: exactly
    one journaled terminal outcome, the same single outcome in both
    peers' adopted logs, at most one image application. Returns
    (digest, violations, evidence): the digest is the pass's
    normalized verdict record, identical across clean passes."""
    violations = {}
    evidence = {'entry_owner': entry_owner}
    admissions = []
    carried = None

    def note(key, diagnostic, detail):
        """Record a violation under this leg's named diagnostics — the
        shared audit helpers report their sibling leg's prefix, so
        the settle names map onto the carry names."""
        if diagnostic == 'demote-settle-uniqueness-nondeterministic':
            diagnostic = 'demote-carry-settle-nondeterministic'
        elif diagnostic == 'demote-settle-uniqueness-failed':
            diagnostic = 'demote-carry-settle-failed'
        violations.setdefault(key, (diagnostic, detail))

    def failed(key, detail):
        note(key, 'demote-carry-settle-failed', detail)

    # The journal floors the audit reads from — a peer that cannot
    # serve its journal cannot evidence the contract.
    floors = {}
    try:
        for name in ('active', 'standby'):
            _, journal = http_json('GET', ctx[name] + '/journal')
            entries = _journal_list(journal)
            floors[name] = (entries[-1].get('seq') or 0) \
                if entries else 0
    except Exception as exc:
        failed('floors', 'a peer\'s journal floor never served: '
               + str(exc)[:200])
        floors = None
    evidence['floors'] = floors

    deadline = time.monotonic() + DEMOTE_CARRY_SETTLE
    owner = wait_for(lambda: _pair_active(ctx), deadline,
                     interval=DEMOTE_CARRY_POLL)
    if owner not in ('active', 'standby'):
        failed('owner', 'no launched peer reports role=active — the '
               'demote race has no field owner')
        return None, violations, evidence
    peer = 'standby' if owner == 'active' else 'active'
    if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                interval=DEMOTE_CARRY_POLL) is None:
        failed('tracking', peer + ' is not a tracking standby — the '
               'carry leg needs a converged peer behind the owner')
        return None, violations, evidence
    record = {'owner': owner, 'peer': peer}
    evidence['pass'] = record
    base, peer_base = ctx[owner], ctx[peer]

    # The targets: a distinct writable point per raced admission so
    # every one is its point's only writer — the pre-window batch
    # lands while the owner legitimately holds the field, the
    # post-window batch into the fenced-but-still-reporting window
    # behind the rogue claim.
    snapshot = _try_snapshot(ctx, base) or {}
    targets = []
    for index, point in enumerate(points):
        baseline = _point_value(snapshot, point)
        if not isinstance(baseline, bool):
            baseline = False
        value = not baseline
        targets.append({
            'point': point, 'value': value, 'baseline': baseline,
            'command': {'write_value': {'point': point, 'kind': 'bool',
                                        'value': {'bool': value}}},
            'actor': 'qa-lane-carry-' + str(number) + '-'
                     + str(index),
            'demoted': owner, 'promoted': peer})

    def race(batch):
        """Fire the receipted submissions back-to-back on the owner;
        each admission that lands records its absolute submission
        index for the carry window — refused ones simply drew no
        admission."""
        for target in batch:
            try:
                target['index'] = _next_receipt_index(ctx, base)
                status, receipt = http_json(
                    'POST', base + '/command',
                    {'command': target['command'],
                     'actor': target['actor']})
            except Exception:
                continue
            record.setdefault('submissions', []).append(
                {'actor': target['actor'], 'status': status,
                 'receipt': receipt})
            if status == 200 \
                    and _outcome_key(receipt) == 'accepted':
                admissions.append(target)

    race(targets[:DEMOTE_CARRY_ADMISSIONS])
    record['admitted'] = len(admissions)
    if not admissions:
        failed('admissions', 'the raced submissions drew no admission '
               '— the leg has nothing to carry across the demote')
        return None, violations, evidence

    # The preemption the #829 family is named for: a rogue writer
    # takes the field claim under the standing owner, so its next
    # boundary meets the fence and demotes it in place — the claim
    # outlives this attachment's close.
    stream = _plant_connect(ctx)
    try:
        preempt = _plant_request(
            stream, {'op': 'claim_writer', 'owner': CLAIM_ROGUE})
    finally:
        stream.close()
    evidence['preempt'] = preempt
    if preempt.get('result') != 'done':
        failed('preempt', 'the rogue claim never preempted the owner '
               '— the demote boundary never ran: '
               + json.dumps(preempt)[:300])
        return None, violations, evidence

    # The fenced-window race: submissions the still-reporting-active
    # gate admits stand pending for the detection scan's suspension —
    # the finding's exact admission shape.
    race(targets[DEMOTE_CARRY_ADMISSIONS:])
    record['post_admitted'] = len(admissions) - record['admitted']

    # The demote-in-place watch: the superseded peer's reported role
    # walks demoting to standby with its monitor answering every
    # poll — a degrade, never a kill.
    watch = {'polls': 0, 'answered': 0, 'roles': []}
    settled = None
    deadline = time.monotonic() + DEMOTE_CARRY_SETTLE
    while time.monotonic() < deadline and settled is None:
        watch['polls'] += 1
        report = _try_role(ctx, base)
        if report is not None:
            watch['answered'] += 1
            watch['roles'].append(report.get('role'))
            if report.get('role') == 'standby':
                settled = report
        if settled is None:
            time.sleep(DEMOTE_CARRY_WATCH)
    evidence['watch'] = watch
    if watch['answered'] != watch['polls']:
        failed('alive', 'the superseded peer\'s monitor stopped '
               'answering mid-demotion — the fenced writer exited '
               'instead of degrading')
        return None, violations, evidence
    if settled is None:
        failed('demote', 'the superseded peer never demoted — its '
               'reported role stayed '
               + json.dumps(watch['roles'][-3:]))
        return None, violations, evidence

    # The carry: the tracking peer's pulls keep adopting the quiesced
    # peer's log — every raced admission must land in its window and
    # at least one still reads Accepted, the suspended admissions the
    # boundary owes a verdict carried live rather than settled
    # provisionally.
    deadline = time.monotonic() + DEMOTE_CARRY_SETTLE
    carried = wait_for(
        lambda: _carry_window(ctx, peer_base, admissions),
        deadline, interval=DEMOTE_CARRY_POLL)
    evidence['carried'] = carried
    if carried is None:
        failed('carry', 'the tracking peer\'s pulls never carried '
               'the suspended admissions — no raced receipt stood '
               'Accepted in its adopted window')
        return None, violations, evidence

    # The successor's promote: the orphaned-but-converged follower's
    # claim preempts the rogue token and its first field-owning
    # boundary applies what the carry held — mid-transition refusals
    # retried inside the settle bound.
    deadline = time.monotonic() + DEMOTE_CARRY_SETTLE
    promoted, last = None, None
    while time.monotonic() < deadline and promoted is None:
        status, body = _settle_call(peer_base + '/promote')
        if status == 200:
            promoted = body
        else:
            last = (status, body)
            time.sleep(DEMOTE_CARRY_POLL)
    record['promote'] = {'body': promoted, 'last_refusal': last}
    if promoted is None:
        failed('promote', 'the carrying peer\'s promote never '
               'succeeded: ' + json.dumps(last)[:300])
        return None, violations, evidence

    # The switch settles: the promoted peer reports active and the
    # superseded one reconverges tracking behind it — the adopted
    # line the audit reads.
    settled = wait_for(
        lambda: (_pair_active(ctx) == peer or None)
        and _tracking_standby(ctx, owner),
        time.monotonic() + DEMOTE_CARRY_SETTLE,
        interval=DEMOTE_CARRY_POLL)
    record['settled'] = settled
    if settled is None:
        failed('settle', 'the carry switch never settled: the '
               'promoted peer\'s role or the superseded peer\'s '
               'reconvergence missed the bound')
        return None, violations, evidence

    evidence['admissions'] = [
        {'actor': admission['actor'], 'point': admission['point'],
         'value': admission['value'], 'baseline': admission['baseline'],
         'index': admission['index'],
         'demoted': admission['demoted'],
         'promoted': admission['promoted']}
        for admission in admissions]

    # The audit: every raced admission resolves to exactly one
    # terminal outcome — applied at a scan boundary or the named
    # superseded rejection — journaled once, carried identically in
    # both peers' adopted logs, and applied to the image at most
    # once. The shared settle-window helpers carry the sibling leg's
    # diagnostic names; `note` remaps them onto this leg's.
    audits = []
    if floors is not None:
        for index, admission in enumerate(admissions):
            audit_deadline = time.monotonic() + DEMOTE_CARRY_AUDIT
            window = wait_for(
                lambda: (lambda w: w if w is not None
                         and _settle_resolved(w, admission)
                         else None)(
                    _settle_window(ctx, admission, floors)),
                audit_deadline, interval=DEMOTE_CARRY_POLL)
            audits.append({'actor': admission['actor'],
                           'window': window})
            if window is None:
                failed('settled-' + admission['actor'],
                       'admission ' + str(admission['actor'])
                       + ' (point ' + str(admission['point'])
                       + ') never reached a terminal journaled '
                       'outcome inside ' + str(DEMOTE_CARRY_AUDIT)
                       + 's')
                continue
            last_writer = all(
                other['point'] != admission['point']
                for other in admissions[index + 1:])
            admission['verdict'] = _settle_judge(
                window, admission, note, check_image=last_writer)
        for item in evidence['admissions']:
            item['verdict'] = next(
                (admission.get('verdict') for admission in admissions
                 if admission['actor'] == item['actor']), None)
    evidence['audit'] = audits
    evidence['violations'] = {
        key: diagnostic for key, (diagnostic, _)
        in violations.items()}
    evidence['digest'] = {
        'outcomes': 'single'
                    if floors is not None and not violations
                    and admissions
                    and all(admission.get('verdict') == 'single'
                            for admission in admissions)
                    else 'diverged',
        'carried': 'some' if carried is not None else 'none'}
    return evidence['digest'], violations, evidence


def scenario_demote_carry_settle(ctx):
    """Exercise single-terminal-outcome settlement across the demote
    carry boundary — the #829 defect family: receipted submissions
    raced on the field owner around a rogue claim's preemption must
    each settle exactly one terminal outcome — applied at a scan
    boundary or the named superseded rejection, never a
    rejected-then-applied pair — with both peers' adopted receipt
    logs carrying the same single outcome and the image reflecting
    at most one application."""
    case = Case(
        'demote-carry-settle',
        'Carried commands settle one terminal outcome',
        'against the deployed pair, receipted writable-point '
        'submissions raced on the field owner around a rogue '
        'claim_writer preemption — the demote-carry boundary the '
        '#829 fix re-suspends rather than provisionally settling — '
        'journal exactly one command_settled outcome per admission '
        '(applied at a scan boundary or the named superseded '
        'rejection, never a rejected-then-applied pair), the '
        'tracking peer\'s pulls carry the suspended admissions '
        'Accepted across the demote, both peers\' adopted receipt '
        'logs carry the same single outcome, the served image '
        'reflects at most one application, and two passes produce '
        'identical digests')
    owner = peer = None
    try:
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries only one endpoint — the pair '
                               'the carry leg needs is absent')
        if ctx.get('plant') is None:
            return case.finish('inconclusive', 'the run publishes no '
                               'plant endpoint for the claim ops')
        for name in ('active', 'standby'):
            try:
                _role(ctx, ctx[name])
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s '
                                   'monitor is unreachable: '
                                   + str(exc)[:200])
        deadline = time.monotonic() + DEMOTE_CARRY_SETTLE
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=DEMOTE_CARRY_POLL)
        if owner is None:
            return case.finish('failed', 'no peer reports role=active')
        peer = 'standby' if owner == 'active' else 'active'
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=DEMOTE_CARRY_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the carry leg has '
                               'no converged peer')
        case.observe('field owner: ' + owner + ' (' + ctx[owner]
                     + '); the carry lands on ' + peer)
        _, signals = http_json('GET', ctx[owner] + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'demote-carry-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming the raced '
                      'writable points')
        points = _writable_bool_points(
            signals, DEMOTE_CARRY_ADMISSIONS + DEMOTE_CARRY_POST)
        if len(points) < DEMOTE_CARRY_ADMISSIONS + DEMOTE_CARRY_POST:
            return case.finish('inconclusive', 'the model declares '
                               'fewer writable bool in-points than '
                               'the carry leg races')
        digests = []
        try:
            for number in (1, 2):
                digest, violations, evidence = _demote_carry_pass(
                    ctx, number, owner, points)
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'demote-carry-pass-' + str(number) + '.json',
                    evidence)
                case.evidence('file', ref, 'demote-carry pass '
                              + str(number) + ' — the preemption, '
                              'demote watch, carried window, '
                              'per-admission audit, and normalized '
                              'digest')
                if violations:
                    diagnostic = 'demote-carry-settle-failed' \
                        if any(name == 'demote-carry-settle-failed'
                               for name, _ in violations.values()) \
                        else 'demote-carry-settle-nondeterministic'
                    return case.finish(
                        'failed', diagnostic + ': ' + '; '.join(
                            detail for _, detail in
                            list(violations.values())[:4]))
                digests.append(digest)
        finally:
            # The pair's entry layout for the cases behind this one —
            # two passes restore it by construction. A pass that died
            # with the rogue claim standing may have left every peer
            # standby: a promotable follower's promote re-takes the
            # field before the documented order restores the roles.
            try:
                current = _pair_active(ctx)
                if current is None:
                    for name in (peer, owner):
                        if name is not None \
                                and _following_standby(
                                    ctx, name) is not None:
                            _settle_call(ctx[name] + '/promote')
                            current = _pair_active(ctx)
                            break
                other = 'standby' if owner == 'active' else 'active'
                if current != owner \
                        and _tracking_standby(ctx, owner) is not None:
                    if current is not None:
                        _settle_call(ctx[current] + '/demote')
                    _settle_call(ctx[owner] + '/promote')
                    wait_for(
                        lambda: (_pair_active(ctx) == owner or None)
                        and _tracking_standby(ctx, other),
                        time.monotonic() + DEMOTE_CARRY_SETTLE,
                        interval=DEMOTE_CARRY_POLL)
                    case.observe('cleanup: restored the entry role '
                                 'layout')
            except Exception as exc:
                case.observe('cleanup: role restore failed: '
                             + str(exc)[:200])
        if digests[0] != digests[1]:
            return case.finish(
                'failed', 'demote-carry-settle-nondeterministic: the '
                'two passes\' digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two demote-carry passes, identical digests')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
