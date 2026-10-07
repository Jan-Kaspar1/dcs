"""The adopted_receipt_regression acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the adopted-stale-receipt leg shares the switch window
# the settled-arbitration leg uses — it needs the settled tracking
# pair, both peers' durable journals, and the entry role layout back
# before the peer-announce and the tune case's a->b switch.
RUNS_AFTER = frozenset({'scenario_settled_receipt_arbitration'})
RUNS_BEFORE = frozenset({'scenario_peer_announce'})


# --------------------------------------------------------------------
# The adopted-stale-receipt-view regression contract — the lane evidence
# for #709's fix serving WW-LCM-001's receipt-as-truth clause: an
# adopted checkpoint's staler receipt view must not regress a locally
# terminal outcome, so `adopt_receipts` cannot move an applied receipt
# back to `accepted` silently and a later covering adoption cannot
# re-settle it and journal a second identical `command_settled`. The
# pair's command audit carries exactly one terminal settle per
# admission.
#
# The finding `adopted-stale-receipt-view-regresses-settle-and-
# rejoournals` (defect; severity low; confidence medium), run
# `qax-20260919-006` against revision cb10f21a on rig `lenovo`, module
# `dcs-monitor`: `record_scan` retained `receipt_outcomes` only over
# the served window and journaled any observed outcome change, and
# `adopt_receipts` clone_from'd the adopted window verbatim. When the
# source's checkpoint carried a staler view of an index, the local
# outcome moved backward silently — `Accepted` journals nothing — and
# the re-settle was then a second observable transition. The pair's
# journals carried the duplicate because the pair syncs the journal:
# receipt idx3 (actor `qa-zombie2`) journaled
# `command_settled applied{tick:61481}` twice on BOTH peers, the second
# landing inside the re-promote boundary but attributed to the original
# settle tick — a replay of the already-recorded verdict, not a new
# settle. A later plain demote+promote cycle produced no third settle,
# so the trigger was the stale-view regression and not the role
# boundaries.
#
# The staging is the finding's own sequence against the deployed pair:
#
# - the settled tracking pair (the field owner active, the sibling
#   tracking) submits a receipted writable-point command and demotes
#   inside its suspension window — the command and the demote inside one
#   scan window — so the receipt persists `Accepted` at the owner's
#   index rather than settling on its own line;
# - the sibling promotes: its boundary's final-sync carry lifts the
#   still-pending admission onto the successor, whose first
#   field-owning scan applies it and journals the settle. The demoted
#   owner's own window has not yet pulled that verdict, so it is the
#   staler view the contract is about — the leg records whether the
#   adoption actually found it and reports the observation as evidence
#   either way, since the pull order between two self-pacing peers is
#   the rig's, not the leg's;
# - the successor demotes again and tracks back, so each line adopts
#   the other's window across the adoption window the finding's
#   duplicate settle landed in.
#
# The audit reads both serving monitors and both durable journals
# throughout: for the one admission, no peer that journaled a settle
# ever serves `accepted` again, every such peer serves the one stable
# terminal verdict, the pair converges on that verdict, and no peer's
# served or durable journal carries a second `command_settled` for it.
#
# Named diagnostics are receipt-regression-failed — the contract never
# performed: a refused switch step, a staging whose demote never
# suspended the receipt, a promotion that never took the field, an
# admission that never settled, or an adoption window carrying no
# terminal verdict — and receipt-regression-nondeterministic —
# the run produced a shape the contract declares impossible: a peer
# serving a regressed `accepted` for a settled admission, the served
# verdict moving under the poll, a peer that recorded the same admission
# twice, or two passes disagreeing. The self-check's
# receipt-regression-unchecked covers the audit itself: each planted
# negative must name its own verdict. Two consecutive passes produce
# identical digests; a run whose served surfaces predate the
# receipt-attribution and checkpoint-window contract, whose ctx carries
# no pair or no durable journals, whose pair never settles tracking, or
# whose staging never lands a suspend/apply/demote sequence reports
# inconclusive.
#
# Inconclusive when the staged run predates the contract — the served
# checkpoint carries no receipt window or admission counters, or the
# served receipt drops the declared actor/reason the audit correlates
# by — or cannot reach the rig the sequence names.

REGRESSION_SETTLE = 45     # bound on each demote, promote, and the
                          # pair's reconvergence inside the leg
REGRESSION_WINDOW = 10     # polls of both peers' served receipts
                          # across the adoption window
REGRESSION_AUDIT = 30      # bound on the settle surfacing in the
                          # journals and each peer's terminal record
REGRESSION_POLL = 0.4      # wait cadence inside the leg
REGRESSION_WATCH = 0.05    # cadence polling the demoting holder —
                          # the demoting walk is one scan
REGRESSION_ATTEMPTS = 3    # suspend/apply staging attempts per pass


def _applied_tick(receipt):
    """The apply tick a settled `applied` receipt carries, or None."""
    applied = ((receipt or {}).get('outcome') or {}).get('applied') or {}
    tick = applied.get('tick')
    return tick if isinstance(tick, int) \
        and not isinstance(tick, bool) else None


def _verdict(receipt):
    """A receipt's normalized verdict — the outcome key beside the
    apply tick, the identity the regression audit compares. The tick
    rides it because the finding's duplicate was a replay of an
    already-recorded `applied{tick}` verdict: a serving log answering
    `applied` without naming the tick cannot tell a re-journaled replay
    from a second settlement."""
    key = _outcome_key(receipt)
    tick = _applied_tick(receipt)
    return key + '@' + str(tick) if tick is not None else key


def _promotable_standby(ctx, name):
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


def _served_admission(ctx, base, admission):
    """The monitor's served receipt for the admission — matched on the
    (command, actor) submission identity — or None while the read
    drops."""
    try:
        receipts, _ = _receipt_window(ctx, base)
    except Exception:
        return None
    return next((entry for entry in receipts
                 if _admission_hit(entry, admission)), None)


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


def _restore_launch_roles(ctx, owner, peer, deadline):
    """Best-effort entry-layout restore inside one bound: demote
    whichever peer holds the field off the entry owner, promote the
    owner once it reports promotable — or promote the sibling as the
    wedge release while no peer owns — and wait for the pair's
    tracking posture."""
    while time.monotonic() < deadline:
        current = _pair_active(ctx)
        if current == owner \
                and _tracking_standby(ctx, peer) is not None:
            return True
        if current is not None and current != owner:
            _settle_call(ctx[current] + '/demote')
            if _promotable_standby(ctx, owner) is not None:
                _settle_call(ctx[owner] + '/promote')
        elif current is None:
            for name in (owner, peer):
                if _promotable_standby(ctx, name) is not None:
                    _settle_call(ctx[name] + '/promote')
                    break
        time.sleep(REGRESSION_POLL)
    return _pair_active(ctx) == owner \
        and _tracking_standby(ctx, peer) is not None


def _adoption_judge(window, admission):
    """The admission's regression verdict from the polled adoption
    window plus its settle counts on both peers' served and durable
    journals.

    A *settler* is a peer whose journal recorded exactly one
    `command_settled` for the admission: the line's own settlement, or
    the adopted record a peer journals when it learns the verdict. The
    contract is about those peers: each must keep serving the terminal
    verdict it recorded — never regressing to the adopted `accepted`
    the finding recorded, and never trading the verdict with the
    peer's — and no peer may record the admission twice. A peer that
    recorded nothing holds no verdict to regress, so it is neither
    required nor blamed; the pair's audit stands once at least one
    line has journaled the admission.

    `held` when every settler holds its one stable terminal verdict;
    `regressed` when a settler served `accepted` at or after its first
    terminal serving — the finding's silent regression; `flapping`
    when a settler served two different terminal verdicts; `doubled`
    when a peer recorded the admission twice in either the served or
    the durable journal; and `silent` when no peer recorded a settle
    or a settle never surfaced in the served window."""
    journaled = window.get('journaled') or {}
    durable = window.get('durable') or {}
    counts = dict(journaled)
    for name, count in durable.items():
        counts[name] = max(counts.get(name, 0), count)
    for count in counts.values():
        if count > 1:
            return 'doubled'
    settlers = sorted(name for name, count in counts.items() if count == 1)
    if not settlers:
        return 'silent'
    polls = window.get('polls') or []
    if not polls:
        return 'silent'
    served_by = {}
    for name in settlers:
        verdicts = [poll.get(name) for poll in polls if poll]
        if not verdicts:
            return 'silent'
        served_by[name] = verdicts
    # The regression clause runs first, over every settler that has
    # served a terminal verdict at all: one that has already answered
    # with it must never fall back to the pending view.
    for verdicts in served_by.values():
        terminal = [verdict for verdict in verdicts
                    if verdict and not verdict.startswith('accepted')]
        if not terminal:
            continue
        if any(verdict and verdict.startswith('accepted')
               for verdict in verdicts[verdicts.index(terminal[0]):]):
            return 'regressed'
    for verdicts in served_by.values():
        terminal = [verdict for verdict in verdicts
                    if verdict and not verdict.startswith('accepted')]
        if not terminal:
            return 'silent'
        if len(set(terminal)) > 1:
            return 'flapping'
    # The pair's one audit converges: every settler ends the window on
    # the same terminal verdict, so the line has one answer for the
    # admission rather than one per peer.
    final = polls[-1]
    answers = set(final.get(name) for name in settlers)
    if len(answers) > 1 or None in answers:
        return 'flapping'
    return 'held'


def _self_check():
    """The leg's unchecked-diagnostic self-test: run the audit judge
    over each planted negative the contract must name — a settler
    still serving the settled admission as `accepted`, the served
    verdict moving under the poll, a journal that recorded the
    admission twice in the served journal or in the durable file, and
    evidence that records no settle at all — and require each to name
    itself. A silent judge answers `held` for all of them and returns
    the negative names it let through."""
    def served(active, standby):
        return [{'active': active, 'standby': standby}]

    def window(polls, journaled=None, durable=None):
        return {'polls': polls,
                'journaled': journaled if journaled is not None
                else {'active': 1, 'standby': 1},
                'durable': durable if durable is not None
                else {'active': 1, 'standby': 1}}

    planted = (
        # The regression itself, in both directions: the settling peer
        # served the terminal verdict, then adopted its peer's staler
        # window and served the settled admission as pending again.
        ('regressed', window(served('applied@61481', 'applied@61481')
                             + served('accepted@61482', 'applied@61481'))),
        ('regressed', window(served('applied@61481', 'applied@61481')
                             + served('applied@61481', 'accepted@61482'))),
        # A settler that never served a terminal verdict at all: the
        # audit holds a settlement nothing serves.
        ('silent', window(served('applied@61481', 'accepted@61482'))),
        # The served verdict moving under the poll — the peer's apply
        # tick is what the duplicate settle replayed.
        ('flapping', window(served('applied@61481', 'applied@61481')
                            + served('applied@61494', 'applied@61494'))),
        # Two peers that never converge on one verdict for the
        # admission.
        ('flapping', window(served('applied@61481', 'applied@61494'))),
        # A peer that recorded the admission twice in its served
        # journal — the finding's duplicate settle.
        ('doubled', window(served('applied@61481', 'applied@61481'),
                           {'active': 2, 'standby': 1})),
        # The durable half's duplicate: the served journal deduped and
        # the journal file did not.
        ('doubled', window(served('applied@61481', 'applied@61481'),
                           None, {'active': 1, 'standby': 2})),
        # No peer recorded a settle: the audit holds nothing.
        ('silent', window(served('applied@61481', 'accepted@61482'),
                          {'active': 0, 'standby': 0},
                          {'active': 0, 'standby': 0})),
        # A settle recorded but never served.
        ('silent', {'polls': [], 'journaled': {'active': 1, 'standby': 1},
                    'durable': {'active': 1, 'standby': 1}}),
    )
    admission = {'actor': 'self-check', 'point': 7, 'value': True,
                 'command': {'write_value': {'point': 7,
                                             'kind': 'bool',
                                             'value': {'bool': True}}}}
    slipped = []
    for name, observed in planted:
        if _adoption_judge(observed, admission) != name:
            slipped.append(name)
    return slipped


def _served_window(ctx, admission):
    """One poll of both peers' served verdict for `admission` —
    `{name: verdict}` — or None while either read drops."""
    window = {}
    for name in ('active', 'standby'):
        receipt = _served_admission(ctx, ctx[name], admission)
        if receipt is None:
            return None
        window[name] = _verdict(receipt)
    return window


def _regression_pass(ctx, number, owner, peer, points):
    """One pass: stage the suspend/apply/demote sequence the finding
    records on the deployed pair, hold the two lines in the adoption
    window the duplicate settle landed in, audit the outcome through
    both serving monitors and both durable journals, then restore the
    entry role layout. Returns (digest, violations, evidence): the
    digest is the pass's normalized verdict record, identical across
    clean passes."""
    violations = {}
    evidence = {'entry_owner': owner, 'attempts': []}
    owner_base, peer_base = ctx[owner], ctx[peer]
    journal_files = ctx.get('journal_files') or {}

    def note(key, diagnostic, detail):
        violations.setdefault(key, (diagnostic, detail))

    def failed(key, detail):
        note(key, 'receipt-regression-failed', detail)

    floors = {}
    for name in ('active', 'standby'):
        try:
            floors[name] = _journal_floor(ctx, name)
        except Exception as exc:
            failed('floors', 'a peer\'s journal floor never served: '
                   + str(exc)[:200])
    if violations:
        return {}, violations, evidence

    # ---- stage the suspended admission -----------------------------
    #
    # The receipted command and the demote land inside one scan
    # window, so the owner's boundary suspends the receipt `Accepted`
    # instead of settling it on its own line — the standing the
    # promotion's final-sync carry then lifts onto the successor.
    staged = None
    for attempt in range(REGRESSION_ATTEMPTS):
        record = {'attempt': attempt}
        evidence['attempts'].append(record)
        deadline = time.monotonic() + REGRESSION_SETTLE
        if _pair_active(ctx) != owner:
            failed('owner-' + str(attempt), 'the entry owner '
                   + owner + ' no longer holds the field (owner '
                   + str(_pair_active(ctx)) + ') — the pass needs the '
                   'settled entry layout to stage on')
            return {}, violations, evidence
        if _promotable_standby(ctx, peer) is None:
            failed('tracking-' + str(attempt), peer
                   + ' is not a promotable standby — the demote the '
                   'staging needs has no converged target')
            return {}, violations, evidence
        offset = attempt % len(points)
        point = points[offset]
        baseline = _point_value(_try_snapshot(ctx, owner_base) or {},
                                point)
        value = not baseline if isinstance(baseline, bool) else True
        actor = 'qa-receipt-regression-' + str(number) + '-' \
            + str(attempt)
        admission = {'actor': actor, 'point': point, 'value': value,
                     'index': _next_receipt_index(ctx, owner_base),
                     'command': {'write_value': {
                         'point': point, 'kind': 'bool',
                         'value': {'bool': value}}},
                     'demoted': owner, 'promoted': peer}
        record.update({'point': point, 'value': value, 'actor': actor})
        try:
            status, receipt = http_json(
                'POST', owner_base + '/command',
                {'command': admission['command'],
                 'actor': actor,
                 'reason': 'adopted-receipt-regression'})
        except Exception as exc:
            record['submission'] = str(exc)[:200]
            continue
        record['submission'] = {'status': status,
                                 'receipt': receipt}
        if status != 200 or _outcome_key(receipt) != 'accepted':
            # The window closed before the admission landed: no
            # suspended standing ever staged, so the attempt restages
            # on the restored layout.
            record['missed'] = 'no admission'
            _restore_launch_roles(ctx, owner, peer,
                                  time.monotonic() + REGRESSION_SETTLE)
            continue
        # The demote inside the receipt's suspension window.
        status, body = _settle_call(owner_base + '/demote')
        record['demote'] = {'status': status, 'body': body}
        if status != 200:
            failed('demote-' + str(attempt), 'the holder\'s demote '
                   'answered ' + str(status) + ': '
                   + json.dumps(body)[:300])
            return {}, violations, evidence
        # The finding's premise, checked on the rig: the receipt
        # persists `accepted` at the owner's index after the demote. A
        # receipt that settled on the owner's own boundary never
        # suspended, so the attempt restages rather than auditing a
        # sequence the contract is not about.
        suspended = wait_for(
            lambda: (_served_admission(ctx, owner_base, admission)
                     is not None) or None,
            deadline, interval=REGRESSION_WATCH)
        standing = _served_admission(ctx, owner_base, admission) \
            if suspended else None
        record['suspended'] = _verdict(standing) if standing else None
        if standing is None or _outcome_key(standing) != 'accepted':
            record['missed'] = 'no suspended standing'
            _restore_launch_roles(ctx, owner, peer,
                                  time.monotonic() + REGRESSION_SETTLE)
            continue
        staged = admission
        break
    if staged is None:
        return {}, {'staging': (
            'receipt-regression-failed',
            'no staging attempt landed the command inside the '
            'holder\'s suspension window — no demote left the '
            'admission\'s receipt pending at the owner\'s index, so '
            'the rig never presented the suspend/apply/demote sequence '
            'the contract audits')}, evidence

    # The promotion whose boundary carry lifts the suspended admission
    # onto the successor, whose first field-owning scan applies it and
    # journals the settle.
    deadline = time.monotonic() + REGRESSION_SETTLE
    status, promote = _settle_call(peer_base + '/promote')
    evidence['promote'] = {'status': status, 'body': promote}
    if status != 200:
        failed('promote', 'the promotion answered ' + str(status) + ': '
               + json.dumps(promote)[:300])
        return {}, violations, evidence
    promoted = wait_for(
        lambda: _pair_active(ctx) == peer or None,
        deadline, interval=REGRESSION_POLL)
    evidence['promoted'] = promoted is not None
    if promoted is None:
        failed('promote-settle', 'the promoted peer never took the '
               'field — the carried admission never had a boundary to '
               'settle at')
        return {}, violations, evidence
    # Wait out the apply: the successor's boundary settles the carried
    # admission, and the finding's regression only matters once the
    # terminal verdict is on record.
    settled = wait_for(
        lambda: (next((verdict for verdict in
                       (_served_window(ctx, staged) or {}).values()
                       if verdict and not verdict.startswith('accepted')),
                      None)),
        time.monotonic() + REGRESSION_AUDIT, interval=REGRESSION_POLL)
    evidence['settled'] = settled
    if settled is None:
        return {}, {'settle': (
            'receipt-regression-failed',
            'the carried admission never settled a terminal verdict on '
            'either peer — the leg has no outcome to keep from '
            'regressing')}, evidence
    # The first poll of the audit: the settling peer serves its own
    # terminal verdict and the predecessor's window is still behind it.
    polls = []
    poll = _served_window(ctx, staged)
    if poll is not None:
        polls.append(poll)

    # The staler view, when the rig produced one: the demoted owner's
    # window has not yet pulled the successor's settlement, so its
    # receipt at the same absolute index still reads `accepted`. The
    # pull order between two self-pacing peers is the rig's, so this is
    # recorded as evidence rather than demanded.
    staler = _served_admission(ctx, owner_base, staged)
    evidence['stale_view'] = {
        'demoted_receipt': _verdict(staler) if staler else None,
        'observed': bool(staler
                         and _outcome_key(staler) == 'accepted')}

    # The second demote: the successor leaves the field and tracks
    # back, so both lines adopt each other's window across the adoption
    # window the finding's duplicate settle landed in.
    status, demote = _settle_call(peer_base + '/demote')
    evidence['adopt_demote'] = {'status': status, 'body': demote}
    if status != 200:
        failed('adopt-demote', 'the successor\'s demote answered '
               + str(status) + ': ' + json.dumps(demote)[:300])
        return {}, violations, evidence
    deadline = time.monotonic() + REGRESSION_SETTLE
    adopted = wait_for(
        lambda: (_promotable_standby(ctx, peer) is not None
                 or _pair_active(ctx) == owner) or None,
        deadline, interval=REGRESSION_POLL)
    evidence['adoption_window'] = adopted is not None

    # ---- the audit -------------------------------------------------
    #
    # Poll both serving monitors across the adoption window, then read
    # both durable journals. The one admission must keep its terminal
    # verdict on both peers without ever regressing to `accepted`, and
    # each peer must hold exactly one `command_settled` for it.
    for _ in range(REGRESSION_WINDOW):
        poll = _served_window(ctx, staged)
        if poll is None:
            break
        polls.append(poll)
        time.sleep(REGRESSION_POLL)
    journaled = {}
    for name in ('active', 'standby'):
        entries = _peer_settles(ctx, name, floors[name], staged)
        if entries is None:
            failed('journal-' + name, name
                   + '\'s served journal never read inside the audit '
                   'window — a lost observation, never a verdict')
            continue
        journaled[name] = len(entries)
    durable = {}
    for name in ('active', 'standby'):
        path = journal_files.get(name)
        if path is None:
            continue
        try:
            durable[name] = len(_durable_settles(path, staged))
        except Exception as exc:
            failed('durable-' + name,
                   name + '\'s durable journal never read: '
                   + str(exc)[:200])
    evidence['polls'] = polls
    evidence['journaled'] = journaled
    evidence['durable'] = durable
    window = {'polls': polls, 'journaled': journaled,
              'durable': durable}
    verdict = _adoption_judge(window, staged)
    evidence['verdict'] = verdict
    label = 'admission ' + str(staged['actor']) + ' (index '
    label += str(staged['index']) + ', point '
    label += str(staged['point']) + ')'
    if verdict == 'silent':
        failed('silent', label + ' carries no single settled verdict '
               'with one command_settled per peer — ' + json.dumps(
                   {'journaled': journaled, 'durable': durable}))
    elif verdict != 'held':
        note('verdict', 'receipt-regression-nondeterministic',
             label + ' reads ' + verdict + ' across the adoption '
             'window — the terminal verdict holds on both peers, one '
             'command_settled per admission per journal')

    # The entry roles: the entry owner takes the field back and the
    # successor tracks it — the layout the cases behind this one
    # enter on.
    restored = _restore_launch_roles(ctx, owner, peer,
                                     time.monotonic() + REGRESSION_SETTLE)
    evidence['restored'] = restored
    if not restored:
        failed('roles', 'the pair did not land back on the entry '
               'roles — owner ' + str(_pair_active(ctx)))
    evidence['digest'] = {
        'outcome': verdict,
        'roles': 'restored' if restored else 'switched',
        'settles': 'one-per-admission'
        if verdict == 'held' else 'broken'}
    return evidence['digest'], violations, evidence


def scenario_adopted_receipt_regression(ctx):
    """Suspend a receipted command at the holder's demote, promote the
    holder's sibling so it applies and journals the settle, demote it
    again so both lines adopt each other's window: the pair's audit
    must keep exactly one command_settled for the admission, the
    served terminal verdict never regressing to accepted on either
    peer."""
    case = Case(
        'adopted-receipt-regression',
        'An adopted stale receipt view cannot regress a settle',
        'with the deployed pair settled and tracking, submit a '
        'receipted writable-point command and demote the holder '
        'inside the receipt\'s suspension window so the receipt '
        'persists accepted, promote the holder\'s sibling so its '
        'boundary carry applies the admission and journals the '
        'settle, then demote the successor again so both lines adopt '
        'each other\'s window: through both serving monitors and '
        'both durable journals the terminal outcome never regresses '
        'to accepted on either peer, exactly one command_settled '
        'stands per admission on each peer\'s journal, /receipts '
        'keeps serving the settled verdict, the pair\'s entry roles '
        'restore, and two passes produce identical digests')
    try:
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries only one endpoint — the pair '
                               'the adoption audit needs is absent')
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
                               'peer — the durable half of the '
                               'regression audit has nothing to read')
        deadline = time.monotonic() + REGRESSION_SETTLE
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=REGRESSION_POLL)
        if owner is None:
            return case.finish('failed', 'no peer reports role=active')
        peer = 'standby' if owner == 'active' else 'active'
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=REGRESSION_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the adoption '
                               'window the regression ran in never '
                               'opens')
        # The contract surface: the owner's checkpoint must carry the
        # receipt window and admission counters the index correlation
        # reads.
        try:
            _, checkpoint = http_json('GET', ctx[owner] + '/checkpoint')
        except Exception as exc:
            return case.finish('inconclusive', 'the field owner\'s '
                               'checkpoint never answered: '
                               + str(exc)[:200])
        if not isinstance(checkpoint, dict) \
                or not isinstance(checkpoint.get('receipts'), list) \
                or not isinstance(
                    (checkpoint.get('command_admission') or {})
                    .get('attempts'), int):
            return case.finish('inconclusive', 'the served checkpoint '
                               'carries no receipt window or admission '
                               'counters — the rig predates the '
                               'adopted-receipt-regression contract')
        _, signals = http_json('GET', ctx[owner] + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'receipt-regression-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming the writable '
                      'command points')
        points = _writable_bool_points(signals, REGRESSION_ATTEMPTS)
        if not points:
            return case.finish('inconclusive', 'the model declares no '
                               'writable bool in-point to admit')
        case.observe('field owner: ' + owner + ' (' + ctx[owner]
                     + '); sibling standby: ' + peer
                     + '; writable points ' + json.dumps(points))
        digests = []
        try:
            for number in (1, 2):
                digest, violations, evidence = _regression_pass(
                    ctx, number, owner, peer, points)
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'receipt-regression-pass-' + str(number) + '.json',
                    evidence)
                case.evidence('file', ref, 'adoption-window pass '
                              + str(number) + ' — the suspend/apply/'
                              'demote staging, the staler-view '
                              'observation, the served polls, the '
                              'settle counts per peer, and the '
                              'normalized digest')
                if violations or digest is None:
                    diagnostic = 'receipt-regression-failed' \
                        if digest is None or any(
                            name == 'receipt-regression-failed'
                            for name, _ in violations.values()) \
                        else 'receipt-regression-nondeterministic'
                    return case.finish(
                        'failed', diagnostic + ': ' + '; '.join(
                            detail for _, detail in
                            list(violations.values())[:4]))
                digests.append(digest)
        finally:
            # The entry layout for the cases behind this one.
            try:
                restored = _restore_launch_roles(
                    ctx, owner, peer,
                    time.monotonic() + REGRESSION_SETTLE)
                if not restored:
                    case.observe('cleanup: the pair did not settle '
                                 'back to the entry roles')
            except Exception as exc:
                case.observe('cleanup: role restore failed: '
                             + str(exc)[:200])
        if digests[0] != digests[1]:
            return case.finish(
                'failed', 'receipt-regression-nondeterministic: '
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
                'receipt-regression-unchecked: the regression audit '
                'stayed silent on planted negatives: '
                + ', '.join(slipped))
        case.observe('the self-check leg\'s planted negatives each '
                     'reported their named diagnostic')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))