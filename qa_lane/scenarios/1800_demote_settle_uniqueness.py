"""The demote_settle_uniqueness acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: The demote-settle-uniqueness case shares that window: it races
# receipted submissions against the documented demote on whichever
# peer owns the field, cycles the switch twice per pass, and lands
# the pair back on the launch roles before the tune case's a->b
# switch.
RUNS_BEFORE = frozenset({'scenario_parameter_tune_carryover'})


# --------------------------------------------------------------------
# The single-terminal-outcome command settlement contract (WW-FND-004's
# receipted-command clause, WW-LCM-001's audit clause) pinned on the rig
# the #685 defect was demonstrated on: a command raced against a demote
# used to settle both rejected{superseded} and applied — one admission,
# two contradictory terminal outcomes. The verified #687 contract
# settles exactly one: the demoted run's pending queue suspends at the
# write gate's close, so a still-`Accepted` admission either rides the
# promote boundary's carry and settles `applied` on the promoted run's
# scan boundary, or the adopted window's submission high-water passes
# it and it settles the named `superseded` rejection — never both,
# never vanished. The leg races receipted writable-point submissions
# against the documented demote on the field-owning peer across
# repeated switch cycles, then audits every admission against both
# peers' journals, adopted receipt logs, and served images: exactly one
# `command_settled` outcome per admission — applied or superseded — the
# same single outcome in each peer's adopted log, and the image
# reflecting at most one application. The named diagnostics are
# demote-settle-uniqueness-failed — the contract never performed: a
# refused switch step, an admission that never reaches a terminal
# journaled outcome — and demote-settle-uniqueness-nondeterministic —
# the run produced an outcome the contract declares impossible: two
# terminal outcomes on one admission, a peer journaling the settle
# twice, diverged adopted logs, a phantom application, or two passes
# disagreeing. The even cycle count lands the pair back on its entry
# roles; two consecutive passes produce identical digests.

DEMOTE_SETTLE_CYCLES = 2       # switch cycles per pass — even, so the
                               # entry role layout restores by
                               # construction
DEMOTE_SETTLE_ADMISSIONS = 3   # receipted submissions raced per cycle
DEMOTE_SETTLE_SETTLE = 45      # bound on each demote/promote and the
                               # pair's reconvergence
DEMOTE_SETTLE_AUDIT = 30       # bound on an admission's terminal
                               # journaled outcome
DEMOTE_SETTLE_POLL = 0.4       # wait cadence inside the leg


def _demote_settle_pass(ctx, number, entry_owner, points):
    """One raced-switch pass: DEMOTE_SETTLE_CYCLES cycles each racing
    DEMOTE_SETTLE_ADMISSIONS receipted writable-point submissions
    against the documented demote on the field-owning peer, then the
    per-admission audit — exactly one journaled terminal outcome, the
    same single outcome in both peers' adopted logs, at most one image
    application. Returns (digest, violations, evidence): the digest is
    the pass's normalized verdict record, identical across clean
    passes."""
    violations = {}
    evidence = {'entry_owner': entry_owner}
    admissions = []
    cycles = []

    def note(key, diagnostic, detail):
        violations.setdefault(key, (diagnostic, detail))

    def failed(key, detail):
        note(key, 'demote-settle-uniqueness-failed', detail)

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

    for cycle in range(DEMOTE_SETTLE_CYCLES):
        if violations:
            break
        record = {'cycle': cycle}
        cycles.append(record)
        deadline = time.monotonic() + DEMOTE_SETTLE_SETTLE
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=DEMOTE_SETTLE_POLL)
        if owner not in ('active', 'standby'):
            failed('owner-' + str(cycle), 'no launched peer reports '
                   'role=active for cycle ' + str(cycle)
                   + ' — the race has no field owner')
            break
        peer = 'standby' if owner == 'active' else 'active'
        record['owner'], record['peer'] = owner, peer
        base, peer_base = ctx[owner], ctx[peer]
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=DEMOTE_SETTLE_POLL) is None:
            failed('tracking-' + str(cycle), peer + ' is not a '
                   'tracking standby — the raced promote has no '
                   'converged target')
            break
        # The cycle's targets and the value each flips to — a distinct
        # slice per cycle keeps every raced admission its point's only
        # writer.
        slice_ = points[cycle * DEMOTE_SETTLE_ADMISSIONS:
                        (cycle + 1) * DEMOTE_SETTLE_ADMISSIONS]
        snapshot = _try_snapshot(ctx, base) or {}
        targets = []
        for index, point in enumerate(slice_):
            baseline = _point_value(snapshot, point)
            if not isinstance(baseline, bool):
                baseline = False
            value = not baseline
            targets.append({
                'point': point, 'value': value, 'baseline': baseline,
                'command': {'write_value': {'point': point,
                                            'kind': 'bool',
                                            'value': {'bool': value}}},
                'actor': 'qa-lane-settle-' + str(number) + '-'
                         + str(cycle) + '-' + str(index),
                'demoted': owner, 'promoted': peer})
        # The race: the receipted submissions land back-to-back on the
        # still-active owner, the documented demote on their heels.
        submissions = []
        for target in targets:
            try:
                status, receipt = http_json(
                    'POST', base + '/command',
                    {'command': target['command'],
                     'actor': target['actor']})
            except Exception as exc:
                status, receipt = None, None
                failed('submit-' + target['actor'],
                       'the raced submission never answered: '
                       + str(exc)[:200])
            submissions.append({'actor': target['actor'],
                                'status': status, 'receipt': receipt})
            if status == 200 \
                    and _outcome_key(receipt) == 'accepted':
                admissions.append(target)
        record['submissions'] = submissions
        record['raced'] = sum(
            1 for submission in submissions
            if _outcome_key(submission.get('receipt')) == 'accepted')
        if record['raced'] == 0 and not violations:
            failed('admissions-' + str(cycle), 'cycle ' + str(cycle)
                   + ' raced no admitted command — every submission '
                   'missed the role gate')
            break
        if violations:
            break
        status, demote = _settle_call(base + '/demote')
        record['demote'] = {'status': status, 'body': demote}
        if status != 200:
            failed('demote-' + str(cycle), 'the field owner\'s demote '
                   'answered ' + str(status) + ': '
                   + json.dumps(demote)[:300])
            break
        # The converged peer promotes — the documented order, mid-
        # transition refusals retried inside the settle bound.
        promoted, last = None, None
        while time.monotonic() < deadline and promoted is None:
            status, body = _settle_call(peer_base + '/promote')
            if status == 200:
                promoted = body
            else:
                last = (status, body)
                time.sleep(DEMOTE_SETTLE_POLL)
        record['promote'] = {'body': promoted, 'last_refusal': last}
        if promoted is None:
            failed('promote-' + str(cycle), 'the converged peer\'s '
                   'promote never succeeded: '
                   + json.dumps(last)[:300])
            break
        # The switch settles: the promoted peer reports active, the
        # demoted one reconverges tracking behind it — the adopted
        # line the audit reads.
        settled = wait_for(
            lambda: (_pair_active(ctx) == peer or None)
            and _tracking_standby(ctx, owner),
            deadline, interval=DEMOTE_SETTLE_POLL)
        record['settled'] = settled
        if settled is None:
            failed('settle-' + str(cycle), 'the switch never settled: '
                   'the promoted peer\'s role or the demoted peer\'s '
                   'reconvergence missed the bound')
            break

    evidence['cycles'] = cycles
    evidence['admissions'] = [
        {'actor': admission['actor'], 'point': admission['point'],
         'value': admission['value'], 'baseline': admission['baseline'],
         'demoted': admission['demoted'],
         'promoted': admission['promoted']}
        for admission in admissions]

    # The audit: every raced admission resolves to exactly one
    # terminal outcome — applied on a scan boundary or the named
    # superseded rejection — journaled once, carried identically in
    # both peers' adopted logs, and applied to the image at most once.
    audits = []
    if floors is not None:
        for index, admission in enumerate(admissions):
            deadline = time.monotonic() + DEMOTE_SETTLE_AUDIT
            window = wait_for(
                lambda: (lambda w: w if w is not None
                         and _settle_resolved(w, admission)
                         else None)(
                    _settle_window(ctx, admission, floors)),
                deadline, interval=DEMOTE_SETTLE_POLL)
            audits.append({'actor': admission['actor'],
                           'window': window})
            if window is None:
                failed('settled-' + admission['actor'],
                       'admission ' + str(admission['actor'])
                       + ' (point ' + str(admission['point'])
                       + ') never reached a terminal journaled '
                       'outcome inside ' + str(DEMOTE_SETTLE_AUDIT)
                       + 's')
                continue
            # The image check holds for the pass's last writer of each
            # point — an earlier superseded value a later admission
            # rewrote is the later one's evidence.
            last_writer = all(
                other['point'] != admission['point']
                for other in admissions[index + 1:])
            admission['verdict'] = _settle_judge(
                window, admission, note, check_image=last_writer)
        for record in evidence['admissions']:
            record['verdict'] = next(
                (admission.get('verdict') for admission in admissions
                 if admission['actor'] == record['actor']), None)
    evidence['audit'] = audits
    evidence['violations'] = {
        key: diagnostic for key, (diagnostic, _)
        in violations.items()}
    restored = _pair_active(ctx) == entry_owner
    evidence['digest'] = {
        'cycles': [{'owner': record.get('owner'),
                    'raced': record.get('raced', 0)}
                   for record in cycles],
        'outcomes': 'single'
                    if floors is not None and not violations
                    and len(admissions)
                    == sum(record.get('raced', 0) for record in cycles)
                    and all(admission.get('verdict') == 'single'
                            for admission in admissions)
                    else 'diverged',
        'roles': 'restored' if restored else 'switched'}
    return evidence['digest'], violations, evidence


def scenario_demote_settle_uniqueness(ctx):
    """Race receipted writable-point submissions against the documented
    demote on the field-owning peer across repeated switch cycles —
    the #685 defect's rig replay: every admission must journal exactly
    one command_settled outcome — applied at a scan boundary or the
    named superseded rejection, never both — with both peers' adopted
    receipt logs carrying the same single outcome and the image
    reflecting at most one application per raced admission."""
    case = Case(
        'demote-settle-uniqueness',
        'Raced commands settle one terminal outcome',
        'against the deployed pair, receipted writable-point '
        'submissions raced against the documented demote on the '
        'field-owning peer across ' + str(DEMOTE_SETTLE_CYCLES)
        + ' switch cycles per pass journal exactly one '
        'command_settled outcome per admission — applied at a scan '
        'boundary or the named superseded rejection, never both — '
        'both peers\' adopted receipt logs carry the same single '
        'outcome per admission, the served image reflects at most one '
        'application per raced admission, the pair returns to its '
        'entry role layout, and two passes produce identical digests')
    try:
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries only one endpoint — the pair '
                               'the race needs is absent')
        for name in ('active', 'standby'):
            try:
                _role(ctx, ctx[name])
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s '
                                   'monitor is unreachable: '
                                   + str(exc)[:200])
        deadline = time.monotonic() + DEMOTE_SETTLE_SETTLE
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=DEMOTE_SETTLE_POLL)
        if owner is None:
            return case.finish('failed', 'no peer reports role=active')
        peer = 'standby' if owner == 'active' else 'active'
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=DEMOTE_SETTLE_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the raced promote '
                               'has no converged target')
        case.observe('field owner: ' + owner + ' (' + ctx[owner]
                     + '); racing the demote against ' + peer)
        _, signals = http_json('GET', ctx[owner] + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'demote-settle-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming the raced '
                      'writable points')
        points = _writable_bool_points(
            signals, DEMOTE_SETTLE_CYCLES * DEMOTE_SETTLE_ADMISSIONS)
        if len(points) < DEMOTE_SETTLE_ADMISSIONS:
            return case.finish('inconclusive', 'the model declares '
                               'fewer writable bool in-points than a '
                               'cycle races')
        digests = []
        try:
            for number in (1, 2):
                digest, violations, evidence = _demote_settle_pass(
                    ctx, number, owner, points)
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'demote-settle-pass-' + str(number) + '.json',
                    evidence)
                case.evidence('file', ref, 'raced-switch pass '
                              + str(number) + ' — the submissions, '
                              'switch answers, per-admission audit, '
                              'and normalized digest')
                if violations:
                    diagnostic = 'demote-settle-uniqueness-failed' \
                        if any(name
                               == 'demote-settle-uniqueness-failed'
                               for name, _ in violations.values()) \
                        else \
                        'demote-settle-uniqueness-nondeterministic'
                    return case.finish(
                        'failed', diagnostic + ': ' + '; '.join(
                            detail for _, detail in
                            list(violations.values())[:4]))
                digests.append(digest)
        finally:
            # The pair's entry layout for the cases behind this one —
            # even cycles restore it by construction; a mid-pass exit
            # gets the documented order run again, best-effort.
            current = _pair_active(ctx)
            other = 'standby' if owner == 'active' else 'active'
            if current != owner \
                    and _tracking_standby(ctx, owner) is not None:
                try:
                    if current is not None:
                        _settle_call(ctx[current] + '/demote')
                    _settle_call(ctx[owner] + '/promote')
                    wait_for(
                        lambda: (_pair_active(ctx) == owner or None)
                        and _tracking_standby(ctx, other),
                        time.monotonic() + DEMOTE_SETTLE_SETTLE,
                        interval=DEMOTE_SETTLE_POLL)
                    case.observe('cleanup: restored the entry role '
                                 'layout')
                except Exception as exc:
                    case.observe('cleanup: role restore failed: '
                                 + str(exc)[:200])
        if digests[0] != digests[1]:
            return case.finish(
                'failed', 'demote-settle-uniqueness-nondeterministic: '
                'the two passes\' digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two raced-switch passes, identical digests')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
