"""The settled_receipt_arbitration acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the settled-receipt-arbitration leg shares the switch
# window the demote-carry and repromote-suspended-settle legs use — it
# needs the settled tracking pair, a raced promotion whose boundary
# pulls inside the field owner's pending window, both peers' durable
# journals, and the entry role layout back before the tune case's a->b
# switch.
RUNS_BEFORE = frozenset({'scenario_parameter_tune_carryover'})


# --------------------------------------------------------------------
# The contradictory-settled-receipt arbitration contract — the lane
# evidence for #690's fix serving WW-LCM-001's receipt-as-truth clause
# and WW-FND-004's named-evidence journal clause. The receipt log is
# documented as the pair's one command audit, converging identically on
# every peer, so adoption of a *settled* receipt must be idempotent or
# arbitrated — never last-pull-wins between contradictory terminal
# verdicts — and a settlement must journal once per admission.
#
# The finding `divergent-settled-receipts-oscillate-flooding-journal`
# (defect; severity high; confidence high), run `qax-20260919-003`
# against revision 2937abf on rig `lenovo`, module `dcs-runtime`: no
# arbitration existed between two different settled outcomes at one
# submission index. Each checkpoint adoption clone_from'd the peer's
# whole log and the recorder treated every differing outcome as a new
# settle, so two peers holding the same admission settled to different
# terminal verdicts handed the index back and forth on every mutual
# adoption at ~5-8 Hz: ~990 phantom `command_settled` lines appended to
# each durable journal.jsonl within minutes, both served rings wrapping
# their 1024 cap and evicting the earlier real audit, and a flapping
# consumer-facing `/receipts` that a single POST /promote ended.
#
# The staging is the reproduction's surviving trigger after the
# quiesced-apply fix (#689): the promotion boundary's final-sync pull
# lands inside the field owner's pending window and carries the
# still-`Accepted` admission onto the successor, which settles it at
# its own boundary while the fenced owner settles the same admission at
# its own. Nothing coordinates those two boundaries, so the pair holds
# one admission settled at one submission index to two different
# terminal verdicts. The field's own arbitration then fences the
# incumbent in place, and the demote that leaves both lines tracking
# each other is the adoption window the ping-pong ran in.
#
# The audit reads both peers' serving monitors and both durable journals
# while they track each other across that window:
#
# - the contradiction was really staged: the same admission journaled
#   two *different* terminal verdicts across the two peers' journals —
#   each line can only journal what it observed, so this is the
#   evidence the arbitration resolved rather than a contract that
#   never ran;
# - the served receipts converge to one arbitrated verdict and stop
#   moving: every poll of `/receipts` on both peers answers the same
#   verdict for every raced admission, so the oscillation never began
#   and the served ring is not churned;
# - each peer's durable journal records at most one `command_settled`
#   per admission, so journal growth is bounded by the admissions and
#   not by the adoptions;
# - the pair reconverges: the converged peer promotes and the pair
#   returns to one active plus one tracking standby, then to its entry
#   role layout.
#
# Named diagnostics are settled-arbitration-failed — the contract never
# performed: a refused switch step, a staging attempt whose promotion
# never answered, a pair that never reconverged, an adoption window
# that carried no served verdict — and
# settled-arbitration-nondeterministic — the run produced a shape the
# contract declares impossible: a served log that kept flapping, two
# peers' served logs still disagreeing, a peer that recorded the same
# admission twice, or two passes disagreeing. The self-check's
# settled-arbitration-unchecked covers the audit itself: each planted
# negative must name its own verdict. Two consecutive passes produce
# identical digests; a run whose served surfaces predate the
# receipt-attribution and checkpoint-window contract, whose ctx
# carries no pair or no durable journals, whose pair never settles
# tracking, or whose staging never lands a promotion pull inside a
# pending window reports inconclusive.

ARBITRATION_SETTLE = 45       # bound on each switch, promote, and
                               # the pair's reconvergence inside the leg
ARBITRATION_WINDOW = 10       # polls of both peers' served receipts
                               # across the adoption window
ARBITRATION_AUDIT = 30        # bound on the contradiction surfacing in
                               # the journals and each admission's
                               # terminal records
ARBITRATION_POLL = 0.4        # wait cadence inside the leg
ARBITRATION_ADMISSIONS = 4    # receipted submissions raced per staging
                               # attempt — a paced owner's pending window
                               # is one apply boundary wide, so a wider
                               # batch raises the chance the promotion's
                               # pull lands inside it
ARBITRATION_ATTEMPTS = 4      # staging attempts per pass


def _applied_tick(receipt):
    """The apply tick a settled `applied` receipt carries, or None."""
    applied = ((receipt or {}).get('outcome') or {}).get('applied') or {}
    tick = applied.get('tick')
    return tick if isinstance(tick, int) \
        and not isinstance(tick, bool) else None


def _verdict(receipt):
    """A receipt's normalized verdict — the outcome key beside the
    apply tick, the identity the convergence audit compares. The tick
    rides it because the finding's two verdicts were the same outcome
    at two apply ticks, and a serving log that answers `applied`
    without naming the tick cannot tell convergence from flapping."""
    key = _outcome_key(receipt)
    tick = _applied_tick(receipt)
    return key + '@' + str(tick) if tick is not None else key


def _followed_standby(ctx, name):
    """The endpoint's report while it is a standby following a source
    — `tracking` behind a field owner, or the `orphaned` verdict a
    line owes while the source it follows owns no field. The
    dual-standby window both peers land in reports `orphaned`: each
    pulls the other, and neither source owns the field. None while the
    peer owns the field or has never adopted a document."""
    try:
        report = _role(ctx, ctx[name])
    except Exception:
        return None
    if report.get('role') != 'standby':
        return None
    sync = report.get('sync') or {}
    return report if 'tracking' in sync or 'orphaned' in sync else None


def _peer_settles(ctx, name, floor, admission):
    """The command_settled receipts the peer's served journal carries
    for `admission` since `floor` — or None while the read drops."""
    try:
        _, journal = http_json('GET', ctx[name] + '/journal?since='
                               + str(floor))
    except Exception:
        return None
    return [receipt for receipt in
            (_journal_settled(entry) for entry in _journal_list(journal))
            if _admission_hit(receipt, admission)]


def _peer_receipts(ctx, name, admission):
    """The peer's served receipt log entries for `admission` — the
    newest last — or None while the read drops."""
    try:
        _, body = http_json('GET', ctx[name] + '/receipts')
    except Exception:
        return None
    return [receipt for receipt in _receipt_list(body)
            if _admission_hit(receipt, admission)]


def _durable_settles(path, admission):
    """The command_settled receipts a `--journal-file` carries for
    `admission` — the durable half of the audit."""
    return [receipt for receipt in
            (_journal_settled(item) for item in _journal_entries(path))
            if _admission_hit(receipt, admission)]


def _journal_floor(ctx, name):
    """The served journal's last seq on one peer — the pass's audit
    floor, or None when the journal cannot be read at all."""
    _, journal = http_json('GET', ctx[name] + '/journal')
    entries = _journal_list(journal)
    return (entries[-1].get('seq') or 0) if entries else 0


def _contradiction(ctx, admissions, floors):
    """The first admission whose two peers journaled *different*
    terminal verdicts, or None. Each line's durable journal records
    what that line observed, so a divergence here is the staged
    contradiction the arbitration has to resolve — the evidence the
    contract ran at all. Each peer's *first* record for the admission
    is its own settlement: a peer that recorded the same admission
    again is the flood the audit below reports, not a second opinion
    on the contradiction."""
    staged = []
    for admission in admissions:
        verdicts = {}
        for name in ('active', 'standby'):
            settles = _peer_settles(ctx, name, floors[name], admission)
            if settles is None:
                return None
            if settles:
                verdicts[name] = _verdict(settles[0])
        if len(verdicts) == 2 and len(set(verdicts.values())) > 1:
            staged.append({'admission': admission,
                           'verdicts': verdicts})
    return staged or None


def _served_window(ctx, admission):
    """One poll of both peers' served verdict for `admission` —
    `{name: verdict}` — or None while either read drops."""
    window = {}
    for name in ('active', 'standby'):
        receipts = _peer_receipts(ctx, name, admission)
        if not receipts:
            return None
        window[name] = _verdict(receipts[-1])
    return window


def _settled_arbitration_judge(window, admission):
    """The admission's arbitration verdict from the polled adoption
    window: `arbitrated` when both peers' served receipts agree on one
    verdict and never moved across the polls, `flapping` when the
    served verdict changed under the poll — the finding's oscillation,
    `diverged` when the two peers still disagree after the window,
    `doubled` when a peer's durable journal recorded the admission
    twice — the finding's flood, and `silent` when the evidence cannot
    judge at all: no peer served a terminal verdict for `admission`
    across the window."""
    polls = window.get('polls') or []
    verdicts = [tuple(sorted(poll.items())) for poll in polls]
    if not verdicts or any(not poll for poll in polls):
        return 'silent'
    if any(verdict.startswith('accepted')
           for poll in polls
           for verdict in poll.values()):
        # A peer still serving the admission as pending: the window
        # never carried a terminal verdict to arbitrate.
        return 'silent'
    if len(set(verdicts)) > 1:
        return 'flapping'
    final = dict(polls[-1])
    if final.get('active') != final.get('standby'):
        return 'diverged'
    for count in (window.get('durable') or {}).values():
        if count > 1:
            return 'doubled'
    return 'arbitrated'


def _self_check():
    """The leg's unchecked-diagnostic self-test: run the audit judge
    over each planted negative the contract must name — the served
    verdict moving under the poll, the two peers still disagreeing,
    a durable journal that recorded the admission twice, and evidence
    that carries no verdict at all — and require each to name itself. A
    silent judge answers `arbitrated` for all of them and returns the
    negative names it let through."""
    admission = {'actor': 'self-check', 'point': 7, 'value': True,
                 'command': {'write_value': {'point': 7,
                                             'kind': 'bool',
                                             'value': {'bool': True}}}}

    def served(active, standby):
        return [{'active': active, 'standby': standby}]

    clean = {'polls': served('applied@1', 'applied@1'),
             'durable': {'active': 1, 'standby': 1}}
    planted = (
        # The served receipt moving under the poll: the ping-pong.
        ('flapping', dict(clean,
                          polls=served('applied@1', 'applied@1')
                          + served('applied@2', 'applied@2'))),
        # The peers still disagreeing after the window.
        ('diverged', dict(clean,
                          polls=served('applied@1', 'applied@4'))),
        # A durable journal that recorded the admission twice.
        ('doubled', {'polls': served('applied@1', 'applied@1'),
                     'durable': {'active': 2, 'standby': 1}}),
        # No served verdict at all: the window carries nothing.
        ('silent', {'polls': [], 'durable': {}}),
        # A peer still serving the admission as pending.
        ('silent', dict(clean,
                        polls=served('applied@1', 'accepted@2'))),
    )
    slipped = []
    for name, window in planted:
        if _settled_arbitration_judge(window, admission) != name:
            slipped.append(name)
    return slipped


def _arbitration_pass(ctx, number, entry_owner, points, journal_files):
    """One pass: stage the contradictory settled pair on the deployed
    pair, hold both lines in the dual-standby adoption window the
    ping-pong ran in, audit the convergence through both serving
    monitors and both durable journals, then promote the converged
    peer and restore the entry roles. Returns (digest, violations,
    evidence): the digest is the pass's normalized verdict record,
    identical across clean passes."""
    violations = {}
    evidence = {'entry_owner': entry_owner, 'attempts': []}
    admissions = []
    staged = []

    def note(key, diagnostic, detail):
        violations.setdefault(key, (diagnostic, detail))

    def failed(key, detail):
        note(key, 'settled-arbitration-failed', detail)

    floors = {}
    for name in ('active', 'standby'):
        try:
            floors[name] = _journal_floor(ctx, name)
        except Exception as exc:
            failed('floors', 'a peer\'s journal floor never served: '
                   + str(exc)[:200])
    if violations:
        return {}, violations, evidence

    # The staging: a receipted batch raced against the promotion whose
    # boundary pull lands inside the field owner's pending window. The
    # field's own arbitration fences the incumbent in place, and the
    # two boundaries settle the carried admission at their own ticks.
    deadline = time.monotonic() + ARBITRATION_SETTLE
    for attempt in range(ARBITRATION_ATTEMPTS):
        if violations:
            break
        record = {'attempt': attempt}
        evidence['attempts'].append(record)
        deadline = time.monotonic() + ARBITRATION_SETTLE
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=ARBITRATION_POLL)
        if owner not in ('active', 'standby'):
            failed('owner-' + str(attempt), 'no launched peer reports '
                   'role=active for attempt ' + str(attempt)
                   + ' — the staging has no field owner')
            break
        peer = 'standby' if owner == 'active' else 'active'
        record['owner'], record['peer'] = owner, peer
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=ARBITRATION_POLL) is None:
            failed('tracking-' + str(attempt), peer + ' is not a '
                   'tracking standby — the raced promote has no '
                   'converged target')
            break
        snapshot = _try_snapshot(ctx, ctx[owner]) or {}
        targets = []
        for offset, point in enumerate(points[:ARBITRATION_ADMISSIONS]):
            baseline = _point_value(snapshot, point)
            targets.append({
                'point': point,
                'value': not baseline if isinstance(baseline, bool)
                         else True,
                'actor': 'qa-lane-arbitration-' + str(number) + '-'
                         + str(attempt) + '-' + str(offset),
                'command': {'write_value': {
                    'point': point, 'kind': 'bool',
                    'value': {'bool': not baseline
                              if isinstance(baseline, bool)
                              else True}}},
                'demoted': owner, 'promoted': peer})
        raced = []
        for target in targets:
            try:
                status, receipt = http_json(
                    'POST', ctx[owner] + '/command',
                    {'command': target['command'],
                     'actor': target['actor']})
            except Exception as exc:
                failed('submit-' + target['actor'],
                       'the raced submission never answered: '
                       + str(exc)[:200])
                break
            if status == 200 \
                    and _outcome_key(receipt) == 'accepted':
                raced.append(target)
        record['raced'] = len(raced)
        if not raced and not violations:
            failed('admissions-' + str(attempt), 'attempt '
                   + str(attempt) + ' raced no admitted command — '
                   'every submission missed the role gate')
            break
        admissions.extend(raced)
        status, promote = _settle_call(ctx[peer] + '/promote')
        record['promote'] = {'status': status, 'body': promote}
        if status != 200:
            failed('promote-' + str(attempt), 'the raced promote '
                   'answered ' + str(status) + ': '
                   + json.dumps(promote)[:300])
            break
        # The contradiction surfaces once both boundaries settled: each
        # line's journal records its own verdict, so the two differ.
        found = wait_for(
            lambda: _contradiction(ctx, admissions, floors), deadline,
            interval=ARBITRATION_POLL)
        if found:
            staged = found
            record['staged'] = [{'actor': entry['admission']['actor'],
                                 'verdicts': entry['verdicts']}
                                for entry in found]
            break
    evidence['admissions'] = [
        {'actor': admission['actor'], 'point': admission['point'],
         'value': admission['value'], 'demoted': admission['demoted'],
         'promoted': admission['promoted']}
        for admission in admissions]
    evidence['staged'] = [
        {'actor': entry['admission']['actor'],
         'verdicts': entry['verdicts']} for entry in staged]
    if not staged and not violations:
        return {}, {'staging': (
            'settled-arbitration-failed',
            'no staging attempt landed the promotion boundary\'s pull '
            'inside the field owner\'s pending window — the rig never '
            'presented the contradictory settled pair the contract '
            'arbitrates')}, evidence

    # The adoption window: the promoted peer's demote leaves both lines
    # standby, each tracking the other, which is the mutual adoption
    # the finding's ping-pong ran in.
    holder = _pair_active(ctx)
    if holder not in ('active', 'standby'):
        failed('holder', 'no peer reports role=active after the staged '
               'promotion — the pair never settled on one holder')
        return {}, violations, evidence
    other = 'standby' if holder == 'active' else 'active'
    status, demote = _settle_call(ctx[holder] + '/demote')
    evidence['demote'] = {'status': status, 'body': demote}
    if status != 200:
        failed('demote', 'the holder\'s demote answered '
               + str(status) + ': ' + json.dumps(demote)[:300])
        return {}, violations, evidence
    deadline = time.monotonic() + ARBITRATION_SETTLE
    dual = wait_for(
        lambda: (_followed_standby(ctx, 'active') is not None
                 and _followed_standby(ctx, 'standby') is not None
                 or None),
        deadline, interval=ARBITRATION_POLL)
    evidence['dual_standby'] = dual is not None
    if dual is None:
        failed('dual-standby', 'the demote never left both peers '
               'following each other as standbys — the adoption '
               'window the contradiction arbitrates in never opened')
        return {}, violations, evidence

    # The audit: poll both serving monitors across the window, then read
    # both durable journals. Every raced admission must converge to one
    # served verdict that never moves, with at most one settle
    # transition recorded per peer.
    polls = []
    for _ in range(ARBITRATION_WINDOW):
        poll = {}
        for admission in admissions:
            seen = _served_window(ctx, admission)
            if seen is None:
                break
            poll[admission['actor']] = seen
        if len(poll) == len(admissions):
            polls.append(poll)
        time.sleep(ARBITRATION_POLL)
    verdicts = {}
    durable = {}
    for admission in admissions:
        window = {'polls': [poll.get(admission['actor'])
                            for poll in polls
                            if admission['actor'] in poll],
                  'durable': {}}
        for name in ('active', 'standby'):
            path = journal_files.get(name)
            if path is None:
                continue
            try:
                count = len(_durable_settles(path, admission))
            except Exception as exc:
                failed('durable-' + name,
                       name + '\'s durable journal never read: '
                       + str(exc)[:200])
                continue
            window['durable'][name] = count
        durable[admission['actor']] = window['durable']
        verdicts[admission['actor']] = _settled_arbitration_judge(
            window, admission)
    evidence['polls'] = polls
    evidence['durable'] = durable
    evidence['verdicts'] = verdicts
    for admission in admissions:
        verdict = verdicts[admission['actor']]
        if verdict == 'silent':
            failed('silent-' + admission['actor'],
                   'admission ' + str(admission['actor']) + ' served '
                   'no terminal verdict on either peer across '
                   + str(ARBITRATION_WINDOW) + ' polls — the '
                   'adoption window carries nothing to arbitrate')
        elif verdict != 'arbitrated':
            note('verdict-' + admission['actor'],
                 'settled-arbitration-nondeterministic',
                 'admission ' + str(admission['actor']) + ' (point '
                 + str(admission['point']) + ') reads ' + verdict
                 + ' across the adoption window — one arbitrated '
                 'verdict, stable on both peers')

    # The pair reconverges: a converged peer takes the field back out
    # of the dual-standby window and the other follows it — one active
    # plus one tracking standby, which on the entry owner is also the
    # run's launch layout again.
    follower = 'standby' if entry_owner == 'active' else 'active'
    status, body = _settle_call(ctx[entry_owner] + '/promote')
    evidence['repromote'] = {'status': status, 'body': body}
    deadline = time.monotonic() + ARBITRATION_SETTLE
    restored = wait_for(
        lambda: (_pair_active(ctx) == entry_owner or None)
        and _tracking_standby(ctx, follower),
        deadline, interval=ARBITRATION_POLL) is not None
    if not restored:
        failed('reconverge', 'the converged peer\'s promote never '
               'returned the pair to one active plus one tracking '
               'standby — owner ' + str(_pair_active(ctx))
               + ', follower ' + str(_tracking_standby(ctx, follower)))
    evidence['restored'] = restored
    evidence['digest'] = {
        'admissions': len(admissions),
        'converged': 'arbitrated'
        if not violations
        and verdicts
        and all(verdict == 'arbitrated'
                for verdict in verdicts.values()) else 'diverged',
        'roles': 'restored' if restored else 'switched',
        'settles': 'one-per-admission'
        if all(count <= 1
               for counts in durable.values()
               for count in counts.values()) else 'flooded',
        'staged': 'contradictory' if staged else 'none'}
    return evidence['digest'], violations, evidence


def scenario_settled_receipt_arbitration(ctx):
    """Race a receipted batch against the promotion whose boundary pull
    lands inside the field owner's pending window, then hold both lines
    in the dual-standby adoption window the #690 finding's ping-pong
    ran in: the pair must converge on one arbitrated verdict for every
    raced admission, journal at most one settle transition per
    admission per peer, and return to one active plus one tracking
    standby on its launch roles."""
    case = Case(
        'settled-receipt-arbitration',
        'Contradictory settled receipts arbitrate to one verdict',
        'against the deployed pair, a receipted batch raced against '
        'the promotion whose boundary pull lands inside the field '
        'owner\'s pending window leaves the carried admission settled '
        'at one submission index to two different terminal verdicts, '
        'and the dual-standby adoption window that follows converges '
        'both peers\' served receipts on one arbitrated verdict that '
        'never moves, records at most one command_settled per '
        'admission per peer\'s durable journal, leaves the pair on one '
        'active plus one tracking standby, and produces identical '
        'digests across two passes')
    try:
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries only one endpoint — the pair '
                               'the arbitration needs is absent')
        for name in ('active', 'standby'):
            try:
                _role(ctx, ctx[name])
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s '
                                   'monitor is unreachable: '
                                   + str(exc)[:200])
        journal_files = ctx.get('journal_files') or {}
        if not all(journal_files.get(name) for name
                   in ('active', 'standby')):
            return case.finish('inconclusive', 'the run context '
                               'declares no durable journal for one '
                               'peer — the bounded-growth half of the '
                               'contract has nothing to read')
        deadline = time.monotonic() + ARBITRATION_SETTLE
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=ARBITRATION_POLL)
        if owner is None:
            return case.finish('failed', 'no peer reports role=active')
        peer = 'standby' if owner == 'active' else 'active'
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=ARBITRATION_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the raced promote '
                               'has no converged target')
        case.observe('field owner: ' + owner + ' (' + ctx[owner]
                     + '); racing the promote against ' + peer)
        _, signals = http_json('GET', ctx[owner] + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'arbitration-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming the raced '
                      'writable points')
        points = _writable_bool_points(signals, ARBITRATION_ADMISSIONS)
        if not points:
            return case.finish('inconclusive', 'the model declares no '
                               'writable bool in-point to race')
        digests = []
        try:
            for number in (1, 2):
                digest, violations, evidence = _arbitration_pass(
                    ctx, number, owner, points, journal_files)
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'arbitration-pass-' + str(number) + '.json',
                    evidence)
                case.evidence('file', ref, 'adoption-window pass '
                              + str(number) + ' — the staging '
                              'attempts, the served polls, the durable '
                              'settle counts, and the normalized digest')
                if violations:
                    diagnostic = 'settled-arbitration-failed' \
                        if any(name == 'settled-arbitration-failed'
                               for name, _ in violations.values()) \
                        else ('settled-arbitration-unchecked'
                              if any(name
                                     == 'settled-arbitration-unchecked'
                                     for name, _ in violations.values())
                              else
                              'settled-arbitration-nondeterministic')
                    return case.finish(
                        'failed', diagnostic + ': ' + '; '.join(
                            detail for _, detail in
                            list(violations.values())[:4]))
                digests.append(digest)
        finally:
            # The entry layout for the cases behind this one.
            current = _pair_active(ctx)
            other = 'standby' if owner == 'active' else 'active'
            if current is not None and current != owner \
                    and _tracking_standby(ctx, owner) is not None:
                try:
                    if current is not None:
                        _settle_call(ctx[current] + '/demote')
                    _settle_call(ctx[owner] + '/promote')
                    wait_for(
                        lambda: (_pair_active(ctx) == owner or None)
                        and _tracking_standby(ctx, other),
                        time.monotonic() + ARBITRATION_SETTLE,
                        interval=ARBITRATION_POLL)
                    case.observe('cleanup: restored the entry role '
                                 'layout')
                except Exception as exc:
                    case.observe('cleanup: role restore failed: '
                                 + str(exc)[:200])
        if digests[0] != digests[1]:
            return case.finish(
                'failed', 'settled-arbitration-nondeterministic: '
                'the two passes\' digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two adoption-window passes, identical digests')
        # The unchecked self-check: each audit clause, run over a
        # planted negative, must name itself — a silent audit can no
        # longer be trusted to catch what it names.
        slipped = _self_check()
        if slipped:
            return case.finish(
                'failed',
                'settled-arbitration-unchecked: the arbitration audit '
                'stayed silent on planted negatives: '
                + ', '.join(slipped))
        case.observe('the self-check leg\'s planted negatives each '
                     'reported their named diagnostic')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
