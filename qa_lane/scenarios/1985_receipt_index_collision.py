"""The receipt_index_collision acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the receipt-index-collision leg shares the preemption
# window the adopted-receipt leg stages through — it needs the settled
# tracking pair, the fenced-but-still-reporting holder, both peers'
# durable journals, and the entry role layout back before the
# peer-announce and the tune case's a->b switch.
RUNS_AFTER = frozenset({'scenario_adopted_receipt_regression'})
RUNS_BEFORE = frozenset({'scenario_peer_announce'})


# --------------------------------------------------------------------
# The submission-index collision audit-preservation contract — the lane
# evidence for #775's fix serving WW-LCM-001's receipt-as-truth clause.
# Admission indices derive from a per-peer attempts counter that
# converges only through checkpoint adoption, so inside the
# promote/fence window both lines can mint receipts at the same absolute
# index for different commands, and whichever line wins convergence
# displaces the other's record. The served `/receipts` must keep a
# servable record of every admitted command rather than replacing a
# settled submission at a reused index — reuse, not aging, which the
# eviction's `receipt_base` signaling does not cover.
#
# The finding `receipt-index-collision-silently-displaces-settled-audit`
# (defect; severity medium; confidence high), reported against the
# promote/fence window on rig `lenovo`, module `dcs-runtime`: the
# demoted peer's adoption clone_from'd the successor's whole window, so
# the raced submission the fence superseded disappeared from the served
# audit and its durable journal alike — only the successor's receipt
# stood at the collided index, with nothing naming the command that
# lost its place.
#
# What the fix names as the displaced admission's preserved audit:
# `Executor::adopt_receipts` adjudicates the collision through the
# merge — the displaced receipt re-mints past the adopted window's
# high-water with its command, actor, and terminal `superseded` verdict
# intact, and the recorder's re-home reports the already-journaled
# settle's new index rather than settling it a second time. So every
# admitted command's terminal verdict stays retrievable on the pair's
# served surface, and exactly one `command_settled` per admission
# stands on each peer's durable journal.
#
# The staging is the promotion boundary's own window:
#
# - the settled tracking pair: the field owner active, the sibling
#   tracking, the sibling's checkpoint converged so its promotion
#   boundary's final-sync fetch meets only the pre-admission document;
# - POST /promote on the standby, then — inside that same window — a
#   receipted writable-point command on the still-field-owning peer, so
#   it is admitted at the index the sibling is about to mint from, its
#   detection scan fences, and the pending receipt is superseded;
# - a second receipted command on the new active minting the same
#   absolute index — the split mint the collision is;
# - the pair then converges and the audit reads both serving monitors
#   and both durable journals.
#
# Named diagnostics are receipt-collision-failed — the contract never
# performed: a refused switch step, a staging whose promotion never
# fenced the holder, an admission that never reached a terminal
# verdict, the displaced admission's terminal verdict missing from the
# pair's served surface and journals where only the successor's receipt
# stands at the collided index, or a settlement no served surface names
# — and
# receipt-collision-nondeterministic — the run produced a shape the
# contract declares impossible: the retrievable verdict moving under
# the poll, two admissions collapsed onto one served record, or a peer
# recording the same admission twice. The self-check's
# receipt-collision-unchecked covers the audit itself: each planted
# negative must name its own verdict. Two consecutive passes produce
# identical digests; a run whose served surfaces predate the
# receipt-attribution and checkpoint-window contract, whose ctx carries
# no pair or no durable journals, whose pair never settles tracking, or
# whose staging never lands the raced admission reports inconclusive.

COLLISION_SETTLE = 45     # bound on each demote, promote, and the
                          # pair's reconvergence inside the leg
COLLISION_WINDOW = 10     # polls of both peers' served receipts
                          # across the convergence window
COLLISION_AUDIT = 30      # bound on the displaced admission's
                          # adjudication surfacing on the pair
COLLISION_POLL = 0.4      # wait cadence inside the leg
COLLISION_WATCH = 0.05    # cadence polling the demoting holder —
                          # the demoting walk is one scan
COLLISION_ATTEMPTS = 3    # raced-admission staging attempts per pass


def _applied_tick(receipt):
    """The apply tick a settled `applied` receipt carries, or None."""
    applied = ((receipt or {}).get('outcome') or {}).get('applied') or {}
    tick = applied.get('tick')
    return tick if isinstance(tick, int) \
        and not isinstance(tick, bool) else None


def _verdict(receipt):
    """A receipt's normalized verdict — the outcome key beside the
    apply tick, the identity the collision audit compares. The tick
    rides it because the pair's two admissions share one index and must
    stay tellable apart by what each line recorded."""
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


def _served_admissions(ctx, base, admission):
    """The monitor's served receipts for one admission — matched on
    the (command, actor) submission identity — or None while the read
    drops."""
    try:
        receipts, _ = _receipt_window(ctx, base)
    except Exception:
        return None
    return [entry for entry in receipts
            if _admission_hit(entry, admission)]


def _served_admission(ctx, base, admission):
    """The monitor's newest served receipt for the admission, or None
    while the read drops or the admission is not served at all."""
    found = _served_admissions(ctx, base, admission)
    if found is None:
        return None
    return found[-1] if found else None


def _settles(ctx, name, floor, admission):
    """The command_settled receipts one peer's served journal carries
    for `admission` since `floor` — or None while the read drops."""
    try:
        _, journal = http_json('GET', ctx[name] + '/journal?since='
                               + str(floor))
    except Exception:
        return None
    return [receipt for receipt in
            (_journal_settled(entry) for entry in _journal_list(journal))
            if _admission_hit(receipt, admission)]


def _file_settles(path, admission):
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
        time.sleep(COLLISION_POLL)
    return _pair_active(ctx) == owner \
        and _tracking_standby(ctx, peer) is not None


def _admission_audit(ctx, window, admission):
    """One polled observation of one admission across the pair: the
    served receipt each peer answers with (None where the admission is
    not served at all), and the `command_settled` records each peer's
    served journal carries since the pass's floor. None while a
    serving monitor drops a read — a lost observation, never the
    audit's verdict."""
    seen = {'served': {}, 'journaled': {}}
    for name in ('active', 'standby'):
        try:
            receipts, _ = _receipt_window(ctx, ctx[name])
            settles = _settles(ctx, name, window['floors'][name],
                               admission)
        except Exception:
            return None
        if settles is None:
            return None
        served = [entry for entry in receipts
                  if _admission_hit(entry, admission)]
        seen['served'][name] = (_verdict(served[-1]) if served
                                else None)
        seen['journaled'][name] = [_verdict(receipt)
                                   for receipt in settles]
    return seen


def _collision_judge(observed, label):
    """The collision audit's verdict over one polled observation of
    both admissions: `preserved` when the displaced admission's terminal
    verdict stays retrievable on the pair's served surface or its
    journals, the colliding successor's own verdict stands beside it,
    the two submissions never collapse onto one served record, and no
    peer recorded either admission twice; `displaced` when the
    displaced admission's terminal verdict is gone — only the
    successor's record stands at the collided index; `collapsed` when
    both admissions answer with one shared served record; `doubled`
    when a peer recorded an admission twice; `instable` when a
    retrievable verdict is not one verdict across the pair; and
    `silent` when neither admission reached a terminal verdict.

    `observed` carries the per-peer served receipt each admission
    answers with (`served`), the settled verdicts each peer's served
    journal and bound `--journal-file` recorded for it (`journaled`,
    `durable`), and the per-peer settle counts (`counts`) the
    once-per-admission bound reads."""
    displaced, successor = observed['displaced'], observed['successor']
    counts = observed.get('counts') or {}
    if any((counts.get(key) or 0) > 1 for key in counts):
        return 'doubled'

    def retrievable(admission):
        """The terminal verdicts naming `admission` anywhere the pair
        keeps its audit — either peer's served receipt, its served
        journal, or its bound journal file."""
        key = 'displaced' if admission is displaced else 'successor'
        found = []
        for name in ('active', 'standby'):
            served = admission['served'].get(name)
            if served and not served.startswith('accepted'):
                found.append(served)
            found.extend(verdict for verdict
                         in admission['journaled'].get(name, [])
                         if verdict and not verdict.startswith('accepted'))
            found.extend(
                verdict for verdict
                in (observed['durable'].get(key) or [])
                if verdict and not verdict.startswith('accepted'))
        return found

    def served_terminal(admission):
        """Whether either peer serves a terminal verdict for
        `admission` — the contract's clause that the served `/receipts`
        keeps a servable record of every admitted command."""
        return [verdict for name in ('active', 'standby')
                for verdict in (admission['served'].get(name),)
                if verdict and not verdict.startswith('accepted')]

    lost = retrievable(displaced)
    if not lost:
        # Only the successor's record stands at the collided index —
        # the silent displacement the finding recorded.
        return 'displaced'
    standing = retrievable(successor)
    if not standing:
        return 'silent'
    for admission in (displaced, successor):
        if not served_terminal(admission):
            # A settlement no served surface names: the journals hold
            # the record, but `/receipts` cannot serve it.
            return 'unserved'
    if len(set(lost)) > 1 or len(set(standing)) > 1:
        return 'instable'
    if _collapsed(observed):
        return 'collapsed'
    return 'preserved'


def _collapsed(observed):
    """Whether both admissions answer with one shared served record —
    the two submissions collapsed onto a single receipt instead of
    standing as two."""
    displaced, successor = observed['displaced'], observed['successor']
    for name in ('active', 'standby'):
        served_displaced = displaced['served'].get(name)
        served_successor = successor['served'].get(name)
        if served_displaced is not None and served_successor is not None:
            continue
        # A peer that serves the displaced admission's record while
        # the successor's own admission has no served record of its own
        # can only be standing in its place.
        if served_displaced is not None and served_successor is None:
            return True
    return False


def _self_check():
    """The leg's unchecked-diagnostic self-test: run the audit judge
    over each planted negative the contract must name — the displaced
    admission's verdict gone where only the successor's record stands,
    both admissions collapsed onto one served record, a peer that
    recorded an admission twice, a retrievable verdict that moved
    under the poll, and evidence carrying no terminal verdict at all —
    and require each to name itself. A silent judge answers
    `preserved` for all of them and returns the negative names it let
    through."""
    applied = {'active': 'applied@61481', 'standby': 'applied@61481'}
    superseded = {'active': 'rejected:superseded',
                  'standby': 'rejected:superseded'}
    gone = {'active': None, 'standby': None}
    none_served = {'active': None, 'standby': 'applied@61481'}

    def observation(served, journaled=None):
        return {'served': dict(served),
                'journaled': {name: list(journaled or [])
                              for name in ('active', 'standby')}}

    def window(displaced, successor, counts=None, durable=None):
        return {
            'displaced': displaced,
            'successor': successor,
            'durable': dict(durable
                            or {'displaced': [], 'successor': []}),
            'counts': dict(counts or {}),
        }

    planted = (
        # The finding's shape: the displaced admission's terminal
        # verdict is nowhere retrievable and only the successor's
        # record stands at the collided index.
        ('displaced', window(observation(gone), observation(applied))),
        # Both admissions answered with the successor's record — a peer
        # standing the displaced submission's verdict where the
        # successor's own admission has no served record.
        ('collapsed', window(observation(superseded),
                             observation(none_served,
                                         ['applied@61481']))),
        # A peer that recorded the displaced admission twice.
        ('doubled', window(observation(superseded), observation(applied),
                           {('displaced', 'active', 'journaled'): 2})),
        # A retrievable verdict that is not one verdict across the
        # pair's surfaces.
        ('instable', window(observation({'active': 'rejected:superseded',
                                         'standby': 'applied@61481'}),
                            observation(applied))),
        # The durable half naming a different verdict than the served
        # surface: the same record read two ways.
        ('instable', window(observation(superseded), observation(applied),
                            durable={'displaced': ['applied@61481'],
                                     'successor': []})),
        # The displaced admission keeps its verdict while the
        # colliding successor never reached one: nothing to preserve
        # beside.
        ('silent', window(observation(superseded),
                          observation({'active': 'accepted@61482',
                                       'standby': 'accepted@61482'}))),
        # The settlement stands in the journals while no served
        # surface names the admission at all.
        ('unserved', window(observation(gone, ['rejected:superseded']),
                            observation(gone, ['applied@61481']))),
    )
    slipped = []
    for name, observed in planted:
        if _collision_judge(observed, 'self-check') != name:
            slipped.append(name)
    return slipped


def _collision_settled(ctx, raced, minted, owner):
    """Whether the served surface carries both admissions' terminal
    verdicts: the racer's own peer serves the displaced one, and the
    colliding successor's verdict is served somewhere on the pair. The
    wait the audit polls before it records its fixed window."""
    displaced = _served_admission(ctx, ctx[owner], raced)
    if displaced is None or _outcome_key(displaced) == 'accepted':
        return False
    for name in ('active', 'standby'):
        successor = _served_admission(ctx, ctx[name], minted)
        if successor is not None \
                and _outcome_key(successor) != 'accepted':
            return True
    return False


def _collision_pass(ctx, number, owner, peer, points):
    """One pass: race a receipted admission on the demoting active
    inside the promotion window, mint the same absolute index on the
    new active, let the pair converge, then audit through both serving
    monitors and both durable journals that every admitted command's
    terminal verdict stays retrievable and each admission settles
    exactly once. Returns (digest, violations, evidence): the digest is
    the pass's normalized verdict record, identical across clean
    passes."""
    violations = {}
    evidence = {'entry_owner': owner, 'attempts': []}
    owner_base, peer_base = ctx[owner], ctx[peer]
    journal_files = ctx.get('journal_files') or {}

    def note(key, diagnostic, detail):
        violations.setdefault(key, (diagnostic, detail))

    def failed(key, detail):
        note(key, 'receipt-collision-failed', detail)

    floors = {}
    for name in ('active', 'standby'):
        try:
            floors[name] = _journal_floor(ctx, name)
        except Exception as exc:
            failed('floors', 'a peer\'s journal floor never served: '
                   + str(exc)[:200])
    if violations:
        return {}, violations, evidence

    # ---- stage the collided mint ----------------------------------
    staged = None
    for attempt in range(COLLISION_ATTEMPTS):
        record = {'attempt': attempt}
        evidence['attempts'].append(record)
        if _pair_active(ctx) != owner:
            failed('owner-' + str(attempt), 'the entry owner ' + owner
                   + ' no longer holds the field (owner '
                   + str(_pair_active(ctx)) + ') — the pass needs the '
                   'settled entry layout to race into')
            return {}, violations, evidence
        if _promotable_standby(ctx, peer) is None:
            failed('tracking-' + str(attempt), peer
                   + ' is not a promotable standby — the preempting '
                   'promotion has no converged target')
            return {}, violations, evidence
        offset = attempt % len(points)
        point = points[offset]
        baseline = _point_value(_try_snapshot(ctx, owner_base) or {},
                                point)
        first = not baseline if isinstance(baseline, bool) else True
        second = not first
        raced_actor = 'qa-receipt-collision-' + str(number) + '-' \
            + str(attempt) + '-raced'
        minted_actor = 'qa-receipt-collision-' + str(number) + '-' \
            + str(attempt) + '-minted'
        raced = {'actor': raced_actor, 'point': point, 'value': first,
                 'demoted': owner, 'promoted': peer,
                 'command': {'write_value': {
                     'point': point, 'kind': 'bool',
                     'value': {'bool': first}}}}
        minted = {'actor': minted_actor, 'point': point,
                  'value': second, 'demoted': peer, 'promoted': owner,
                  'command': {'write_value': {
                      'point': point, 'kind': 'bool',
                      'value': {'bool': second}}}}
        record.update({'point': point, 'raced': raced_actor,
                       'minted': minted_actor})
        # The absolute index each line will mint at, read *before*
        # either submits — the submission is what mints there, so a
        # read after it names the successor of the contested position.
        # The holder's read precedes the promotion because every read
        # drives a scan, and the holder's next scan is the fenced
        # detection scan that closes its reporting window.
        raced['index'] = _next_receipt_index(ctx, owner_base)
        # The promotion boundary's final-sync fetch meets the
        # pre-admission document, and its claim preempts the holder.
        status, body = _settle_call(peer_base + '/promote')
        record['promote'] = {'status': status, 'body': body}
        if status != 200:
            failed('promote-' + str(attempt), 'POST /promote on ' + peer
                   + ' answered ' + str(status) + ': '
                   + json.dumps(body)[:300])
            return {}, violations, evidence
        # The successor's own high-water: the promotion's carry lifted
        # nothing here, so the split mint is the shape the contract
        # adjudicates only when the two lines mint at one index.
        minted['index'] = _next_receipt_index(ctx, peer_base)
        record['indices'] = {'raced': raced['index'],
                             'minted': minted['index']}
        if minted['index'] != raced['index']:
            record['missed'] = 'no split mint'
            _restore_launch_roles(ctx, owner, peer,
                                  time.monotonic() + COLLISION_SETTLE)
            continue
        # The raced admission, fired into the fenced-but-still-
        # reporting holder's window — it admits the command at the
        # index the sibling is about to mint from, and its detection
        # scan fences and demotes it in place.
        try:
            status, receipt = http_json(
                'POST', owner_base + '/command',
                {'command': raced['command'],
                 'actor': raced_actor,
                 'reason': 'receipt-index-collision'})
        except Exception as exc:
            record['submission'] = str(exc)[:200]
            _restore_launch_roles(ctx, owner, peer,
                                  time.monotonic() + COLLISION_SETTLE)
            continue
        record['submission'] = {'status': status, 'receipt': receipt}
        if status != 200 or _outcome_key(receipt) != 'accepted':
            record['missed'] = 'no admission'
            _restore_launch_roles(ctx, owner, peer,
                                  time.monotonic() + COLLISION_SETTLE)
            continue
        # The successor's own admission at the same absolute index: the
        # split mint the collision is.
        try:
            status, receipt = http_json(
                'POST', peer_base + '/command',
                {'command': minted['command'],
                 'actor': minted_actor,
                 'reason': 'receipt-index-collision'})
        except urllib.error.HTTPError as exc:
            failed('successor-' + str(attempt),
                   'the successor\'s submission was refused: '
                   + str(exc)[:200])
            return {}, violations, evidence
        record['successor'] = {'status': status, 'receipt': receipt}
        if status != 200:
            failed('successor-' + str(attempt),
                   'the successor\'s submission answered '
                   + str(status) + ': ' + json.dumps(receipt)[:300])
            return {}, violations, evidence
        deadline = time.monotonic() + COLLISION_SETTLE
        demoted = wait_for(
            lambda: ((_role(ctx, owner_base) or {}).get('role')
                     == 'standby') or None,
            deadline, interval=COLLISION_WATCH)
        record['holder_demoted'] = demoted is not None
        staged = (raced, minted)
        break
    evidence['attempts'] = evidence['attempts']
    if staged is None:
        return {}, {'staging': (
            'receipt-collision-failed',
            'no staging attempt landed the raced admission inside the '
            'promotion window — the holder closed its reporting window '
            'before the submission did, so the rig never presented the '
            'split mint the contract adjudicates')}, evidence

    raced, minted = staged
    evidence['displaced'] = {'actor': raced['actor'],
                             'point': raced['point'],
                             'value': raced['value']}
    evidence['successor'] = {'actor': minted['actor'],
                             'point': minted['point'],
                             'value': minted['value']}

    # The convergence: wait out the adjudication, then record a fixed
    # window of observations so the digest and the evidence are
    # comparable across passes.
    settled = wait_for(
        lambda: _collision_settled(ctx, raced, minted, owner) or None,
        time.monotonic() + COLLISION_AUDIT, interval=COLLISION_POLL)
    evidence['settled'] = settled is not None
    polls = []
    for _ in range(COLLISION_WINDOW):
        read = {'displaced': _admission_audit(
                    ctx, {'floors': floors}, raced),
                'successor': _admission_audit(
                    ctx, {'floors': floors}, minted)}
        if read['displaced'] is None or read['successor'] is None:
            break
        polls.append(read)
        time.sleep(COLLISION_POLL)

    durable = {}
    for name in ('active', 'standby'):
        path = journal_files.get(name)
        if path is None:
            continue
        try:
            durable[(name, 'displaced')] = [
                _verdict(receipt) for receipt in _file_settles(path, raced)]
            durable[(name, 'successor')] = [
                _verdict(receipt) for receipt in _file_settles(path, minted)]
        except Exception as exc:
            failed('durable-' + name,
                   name + '\'s durable journal never read: '
                   + str(exc)[:200])
    if not polls:
        failed('window', 'the audit\'s window never read — a serving '
               'monitor dropped its receipts or journal inside the '
               'bound')
        evidence['digest'] = {'displaced': 'silent',
                              'roles': 'unrestored',
                              'settles': 'broken'}
        return evidence['digest'], violations, evidence
    final = polls[-1]
    counts = {}
    for key in ('displaced', 'successor'):
        for name in ('active', 'standby'):
            counts[(key, name, 'journaled')] = len(
                final[key]['journaled'].get(name) or [])
            counts[(key, name, 'durable')] = len(
                durable.get((name, key)) or [])
    observed = {
        'displaced': final['displaced'],
        'successor': final['successor'],
        'durable': {'displaced': durable.get((owner, 'displaced'), []),
                    'successor': durable.get((peer, 'successor'), [])},
        'counts': counts}
    evidence['polls'] = polls
    evidence['counts'] = {str(key): value
                          for key, value in observed['counts'].items()}
    evidence['verdict'] = _collision_judge(observed, 'the collision')

    label = 'the displaced admission ' + str(raced['actor']) \
        + ' (point ' + str(raced['point']) + ')'
    verdict = evidence['verdict']
    if verdict == 'silent':
        failed('settle', label + ' and its colliding successor never '
               'reached a terminal verdict — the contract has nothing '
               'to preserve')
    elif verdict == 'unserved':
        failed('unserved', label + ' carries a settlement no served '
               'surface names — the pair\'s journals hold its terminal '
               'verdict while /receipts cannot serve it')
    elif verdict == 'displaced':
        failed('displaced', label + ' has no retrievable terminal '
               'verdict on the pair\'s served surface or journals '
               'where only the colliding successor\'s receipt stands '
               'at the index — a settled submission silently '
               'replaced')
    elif verdict != 'preserved':
        note('verdict', 'receipt-collision-nondeterministic',
             label + ' reads ' + verdict + ' across the convergence '
             'window — every admitted command keeps its retrievable '
             'terminal verdict and one command_settled per admission '
             'per peer')

    # The entry roles: the entry owner takes the field back and the
    # successor tracks it.
    restored = _restore_launch_roles(ctx, owner, peer,
                                     time.monotonic() + COLLISION_SETTLE)
    evidence['restored'] = restored
    if not restored:
        failed('roles', 'the pair did not land back on the entry '
               'roles — owner ' + str(_pair_active(ctx)))
    evidence['digest'] = {
        'displaced': verdict,
        'roles': 'restored' if restored else 'switched',
        'settles': 'one-per-admission' if verdict == 'preserved'
        else 'broken'}
    return evidence['digest'], violations, evidence


def scenario_receipt_index_collision(ctx):
    """Race a receipted admission on the demoting active inside the
    promotion window and mint the same absolute index on the new
    active: the collided admission's terminal verdict must stay
    retrievable on the pair's served surface, never silently replaced
    by its successor's receipt at that index."""
    case = Case(
        'receipt-index-collision',
        'A collided submission index preserves both admissions',
        'with the deployed pair settled and tracking, promote the '
        'tracking sibling so its boundary fetch meets the '
        'pre-admission document and its claim preempts the holder, '
        'fire a receipted writable-point command at the still-field-'
        'owning peer inside that window so the fence supersedes it, '
        'then submit a second receipted command on the new active '
        'minting the same absolute index: after the pair converges, '
        'through both serving monitors and both durable journals '
        'every admitted command keeps its retrievable terminal '
        'verdict — the displaced one re-minted with its superseded '
        'verdict, never silently replaced at the collided index — '
        'exactly one command_settled stands per admission on each '
        'peer\'s journal, the pair\'s entry roles restore, and two '
        'passes produce identical digests')
    try:
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries only one endpoint — the pair '
                               'the collision audit needs is absent')
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
                               'collision audit has nothing to read')
        deadline = time.monotonic() + COLLISION_SETTLE
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=COLLISION_POLL)
        if owner is None:
            return case.finish('failed', 'no peer reports role=active')
        peer = 'standby' if owner == 'active' else 'active'
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=COLLISION_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the preempting '
                               'promotion the collision races into '
                               'has no converged target')
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
                               'receipt-index-collision contract')
        _, signals = http_json('GET', ctx[owner] + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'receipt-collision-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming the writable '
                      'command points')
        points = _writable_bool_points(signals, COLLISION_ATTEMPTS)
        if not points:
            return case.finish('inconclusive', 'the model declares no '
                               'writable bool in-point to admit')
        case.observe('field owner: ' + owner + ' (' + ctx[owner]
                     + '); sibling standby: ' + peer
                     + '; writable points ' + json.dumps(points))
        digests = []
        try:
            for number in (1, 2):
                digest, violations, evidence = _collision_pass(
                    ctx, number, owner, peer, points)
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'receipt-collision-pass-' + str(number) + '.json',
                    evidence)
                case.evidence('file', ref, 'collision pass '
                              + str(number) + ' — the preempting '
                              'promote, the raced and minted '
                              'admissions, the convergence polls, the '
                              'per-admission settle counts, and the '
                              'normalized digest')
                if violations or digest is None:
                    diagnostic = 'receipt-collision-failed' \
                        if digest is None or any(
                            name == 'receipt-collision-failed'
                            for name, _ in violations.values()) \
                        else 'receipt-collision-nondeterministic'
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
                    time.monotonic() + COLLISION_SETTLE)
                if not restored:
                    case.observe('cleanup: the pair did not settle '
                                 'back to the entry roles')
            except Exception as exc:
                case.observe('cleanup: role restore failed: '
                             + str(exc)[:200])
        if digests[0] != digests[1]:
            return case.finish(
                'failed', 'receipt-collision-nondeterministic: '
                'the two passes\' digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two convergence passes, identical digests')
        # The unchecked self-check: each audit clause, run over a
        # planted negative, must name itself.
        slipped = _self_check()
        if slipped:
            return case.finish(
                'failed',
                'receipt-collision-unchecked: the collision audit '
                'stayed silent on planted negatives: '
                + ', '.join(slipped))
        case.observe('the self-check leg\'s planted negatives each '
                     'reported their named diagnostic')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))