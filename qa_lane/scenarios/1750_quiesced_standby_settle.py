"""The quiesced_standby_settle acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the quiesced-standby-settle leg sits in the launch-layout
# window the standby-loss leg opens — it needs the settled tracking
# pair, the driven third-controller seam, and both durable journals,
# and it restores the launch roles before the tune case's a->b
# switch.
RUNS_BEFORE = frozenset({'scenario_parameter_tune_carryover'})


# --------------------------------------------------------------------
# The quiesced-standby no-phantom-settle contract — the lane evidence
# for #689's fix serving WW-LCM-001's receipt-as-truth clause and the
# write gate's quiescence rule. A tracking standby's quiesced scan
# must never settle an adopted pending command on its gated image:
# the defect applied the adopted receipt on the staged image, minted
# a phantom settled verdict that journaled before any real boundary,
# and was never re-executed on the live line. The contract: while
# the line still owes the admission a verdict, the gated image takes
# nothing, the serving monitor carries no terminal receipt, and no
# command_settled journals ahead of the boundary; at the promotion
# boundary the carried command resolves exactly once — the adopted
# record carrying the line's applied verdict verbatim, never a
# fresh local mint.
#
# Staging a standby that holds an adopted still-pending receipt
# takes the run's driven third controller: a paced standby's pulls
# are continuous, so its pending window is one apply boundary wide
# and unobservable; the driven peer's pulls run only inside
# POST /scan, so the leg lands its pull inside the owner's pending
# window and the adopted receipt stays still-pending — carried — for
# the whole observation window between the pull and the promote.
# The deployed sibling's covering adoption — continuously converged
# — is audited ex post in the same evidence window: exactly one
# journaled settle carrying the line's verdict.
#
# The audit reads the quiesced standby's serving monitor and its
# durable journal while it holds the adopted pending receipt: no
# terminal outcome, no journaled settle, the staged image
# unchanged. The promote boundary then resolves the carried command
# exactly once on every peer — the field owner's own apply plus each
# follower's one adopted record at the line's apply tick — and the
# serving logs and images agree. Two consecutive passes produce
# identical digests; the self-check plants a phantom settle minted
# on the gated image through the pending audit and a boundary that
# never resolved through the verdict — a silent audit reports
# quiesced-settle-unchecked. Inconclusive when the staged run
# predates the contract — no receipt-level actor/reason
# attribution, no checkpoint receipt window or admission counters,
# no driven-controller seam — or cannot reach the rig it names.

QUIESCED_SETTLE = 45    # bound on each converge, promote, and
                        # restore inside the leg
QUIESCED_AUDIT = 30     # bound on the line's boundary settle and
                        # each peer's adopted record journaling
QUIESCED_POLL = 0.4     # wait cadence inside the leg
QUIESCED_EDGE = 1.0     # bound on one scan-boundary wait — the
                        # staging lands its pull inside the pending
                        # window the boundary just opened
QUIESCED_ATTEMPTS = 8   # staged-adoption bound — a driven pull that
                        # lands past the owner's apply boundary
                        # restages
QUIESCED_SCANS = 4      # driven scans per batch


def _tracking(report):
    """Whether a /role report shows a tracking standby — the
    promotable posture the boundary's restore owes."""
    return (report or {}).get('role') == 'standby' \
        and 'tracking' in ((report or {}).get('sync') or {})


def _drive(ctx, scans=QUIESCED_SCANS):
    """One POST /scan batch on the driven peer — every pull it ever
    performs happens inside the request."""
    return http_json('POST', ctx['driven'] + '/scan',
                     {'scans': scans}, timeout=scans * 2 + 15)


def _applied_tick(receipt):
    """The apply tick a settled `applied` receipt carries, or None —
    the line's boundary stamp an adopted record must carry
    verbatim."""
    applied = ((receipt or {}).get('outcome') or {}).get('applied') \
        or {}
    tick = applied.get('tick')
    return tick if isinstance(tick, int) \
        and not isinstance(tick, bool) else None


def _standing_receipt(ctx, base, admission):
    """The newest receipt matching the admission in the peer's
    served log — the (command, actor) pair is unique per submission —
    or None while the window carries none or the read dropped."""
    try:
        _, body = http_json('GET', base + '/receipts')
    except Exception:
        return None
    matches = [receipt for receipt in _receipt_list(body)
               if _admission_hit(receipt, admission)]
    return matches[-1] if matches else None


def _standing_settles(ctx, base, floor, admission):
    """The command_settled receipts matching the admission in the
    peer's /journal tail since the pass's floor — or None while the
    read dropped."""
    try:
        _, journal = http_json('GET', base + '/journal?since='
                               + str(floor))
    except Exception:
        return None
    return [receipt for receipt in
            (_journal_settled(entry)
             for entry in _journal_list(journal))
            if _admission_hit(receipt, admission)]


def _durable_settles(path, admission):
    """The command_settled receipts matching the admission in a
    --journal-file's parsed records — the durable half of the
    audit."""
    return [receipt for receipt in
            (_journal_settled(item)
             for item in _journal_entries(path))
            if _admission_hit(receipt, admission)]


def _quiesced_window(ctx, admission, floors):
    """One polled audit snapshot: each peer's journaled
    command_settled matches, adopted-log receipts, and served image
    value for the admission's point since the pass's journal floors.
    None while any peer drops a read — a lost observation, never the
    audit's verdict."""
    window = {'journaled': {}, 'logged': {}, 'images': {}}
    for name in ('active', 'standby', 'driven'):
        if ctx.get(name) is None:
            continue
        try:
            _, journal = http_json('GET', ctx[name]
                                   + '/journal?since='
                                   + str(floors[name]))
            _, receipts = http_json('GET', ctx[name] + '/receipts')
            snapshot = _snapshot(ctx, ctx[name])
        except Exception:
            return None
        window['journaled'][name] = [
            receipt for receipt in
            (_journal_settled(entry)
             for entry in _journal_list(journal))
            if _admission_hit(receipt, admission)]
        window['logged'][name] = [
            receipt for receipt in _receipt_list(receipts)
            if _admission_hit(receipt, admission)]
        window['images'][name] = _point_value(snapshot,
                                              admission['point'])
    return window


def _quiesced_pending(held, admission, note):
    """The pending-window verdict: `held` is one read of the
    quiesced standby while it carries the adopted still-pending
    receipt — {'receipt': its served receipt, 'journaled': its
    monitor's journaled matches, 'durable': its --journal-file
    matches, 'image': the point's served value}. The contract owes
    nothing on the gated image while the line still owes the
    admission a verdict — any terminal receipt, any journaled
    settle, or the pending value on the staged image is the phantom
    the fix exists to prevent. Returns 'held' or 'phantom'."""
    label = 'the carried admission ' + str(admission['actor']) \
        + ' (point ' + str(admission['point']) + ')'
    verdict = 'held'
    receipt = held.get('receipt')
    if receipt is not None \
            and _outcome_key(receipt) != 'accepted':
        note('quiesced-verdict', 'quiesced-settle-failed',
             label + ' reads ' + _outcome_key(receipt)
             + ' on the quiesced standby\'s serving monitor — a '
             'terminal verdict minted on the gated image while '
             'the line still owes it one')
        verdict = 'phantom'
    for name in ('journaled', 'durable'):
        entries = held.get(name) or []
        if entries:
            note('quiesced-' + name, 'quiesced-settle-failed',
                 label + ' shows ' + str(len(entries))
                 + ' command_settled records on the quiesced '
                 'standby\'s ' + name + ' journal ahead of the '
                 'boundary: ' + json.dumps(
                     [_outcome_key(receipt)
                      for receipt in entries]))
            verdict = 'phantom'
    if held.get('image') == admission['value']:
        note('quiesced-image', 'quiesced-settle-failed',
             label + '\'s pending write landed on the quiesced '
             'standby\'s gated image — the staged snapshot serves '
             + json.dumps(held.get('image')))
        verdict = 'phantom'
    return verdict


def _quiesced_boundary(journaled, logged, images, line, admission,
                       note):
    """The promotion-boundary verdict: `journaled` and `logged` map
    each peer's matching command_settled receipts and served-log
    receipts, `images` each peer's point value, and `line` is the
    field owner's journaled settle — the true boundary's record
    every adopted resolution must carry verbatim. Exactly one
    applied per peer at the line's apply tick: a missing settle is
    the carried command never resolved, a duplicate or divergent
    verdict the phantom contract's other face. Returns 'single' or
    'diverged'."""
    label = 'the carried admission ' + str(admission['actor']) \
        + ' (point ' + str(admission['point']) + ')'
    verdict = 'single'
    line_tick = _applied_tick(line)
    for name in sorted(journaled):
        entries = journaled[name]
        if len(entries) != 1:
            note('count-' + name,
                 'quiesced-settle-failed' if not entries
                 else 'quiesced-settle-nondeterministic',
                 label + ' journaled ' + str(len(entries))
                 + ' command_settled records on ' + name
                 + ' — the boundary resolves the carried command '
                 'exactly once there')
            verdict = 'diverged'
            continue
        receipt = entries[0]
        if _outcome_key(receipt) != 'applied':
            note('verdict-' + name,
                 'quiesced-settle-nondeterministic',
                 label + ' settled ' + _outcome_key(receipt)
                 + ' on ' + name + ' — the line applied it, so the '
                 'carried command\'s only resolution is applied')
            verdict = 'diverged'
        elif line_tick is not None \
                and _applied_tick(receipt) != line_tick:
            note('tick-' + name,
                 'quiesced-settle-nondeterministic',
                 label + ' settled applied on ' + name
                 + ' at tick ' + str(_applied_tick(receipt))
                 + ' where the line\'s boundary applied it at '
                 + str(line_tick) + ' — a fresh mint on this peer, '
                 'never the adopted record')
            verdict = 'diverged'
    for name in sorted(logged):
        served = logged[name]
        if len(served) != 1 \
                or _outcome_key(served[0]) != 'applied':
            note('log-' + name, 'quiesced-settle-failed',
                 name + '\'s adopted log carries '
                 + str(len(served)) + ' receipts for ' + label
                 + ' (' + json.dumps(
                     [_outcome_key(receipt) for receipt in served])
                 + ') where the journal settled applied')
            verdict = 'diverged'
    for name in sorted(images):
        if images[name] != admission['value']:
            note('image-' + name, 'quiesced-settle-failed',
                 name + '\'s image never took ' + label
                 + '\'s applied write — serves '
                 + json.dumps(images[name]))
            verdict = 'diverged'
    return verdict


def _quiesced_pass(ctx, number, owner, peer, point):
    """One quiesced-standby pass: stage the carried admission — the
    receipted writable-point command the driven standby's frozen
    pulls adopt still pending — audit the gated image through its
    serving monitor and durable journal while the receipt stands
    accepted, promote the standby, audit the boundary's
    exactly-once resolution, and restore the launch roles. Returns
    (digest, violations, evidence): digest is the pass's normalized
    verdict record, identical across clean passes."""
    violations = {}
    evidence = {'owner': owner, 'pass': number}
    digest = {'adopted': 'unstaged', 'gated': 'unaudited',
              'boundary': 'unseen', 'roles': 'unrestored'}
    base, peer_base, driven_base = \
        ctx[owner], ctx[peer], ctx['driven']
    journal_files = ctx.get('journal_files') or {}

    def note(key, diagnostic, detail):
        violations.setdefault(key, (diagnostic, detail))

    def failed(key, detail):
        note(key, 'quiesced-settle-failed', detail)

    def inconclusive(key, detail):
        evidence['inconclusive'] = detail
        return finish(None)

    def finish(result):
        evidence['violations'] = {key: {'diagnostic': name,
                                        'detail': detail}
                                  for key, (name, detail)
                                  in violations.items()}
        evidence['digest'] = result
        return result, violations, evidence

    # The settled gate: the entry owner holds the field and the
    # sibling tracks it — the layout the pass's restore owes.
    if _pair_active(ctx) != owner \
            or _tracking_standby(ctx, peer) is None:
        failed('settle', 'the pair never settled — ' + owner
               + ' holds no active role with ' + peer
               + ' tracking behind it')
        return finish(None)

    # Converge the driven peer onto the owner — every batched scan
    # is a checkpoint pull, the cadence the staged adoption lands
    # inside the pending window.
    converged = None
    deadline = time.monotonic() + QUIESCED_SETTLE
    while converged is None and time.monotonic() < deadline:
        try:
            _drive(ctx)
        except Exception as exc:
            failed('driven-scan', 'the driven peer\'s /scan batch '
                   'never answered: ' + str(exc)[:200])
            return finish(None)
        converged = _tracking_standby(ctx, 'driven')
        if converged is None:
            time.sleep(QUIESCED_POLL)
    evidence['driven_converged'] = converged
    if converged is None:
        failed('driven-converge', 'the driven peer never reported '
               'a tracking standby on ' + owner)
        return finish(None)

    # The journal floors the audit reads from — a peer that cannot
    # serve its journal cannot evidence the contract.
    floors = {}
    try:
        for name in (owner, peer, 'driven'):
            floors[name] = _journal_cursor(ctx, ctx[name])
    except Exception as exc:
        failed('floors', 'a peer\'s journal floor never served: '
               + str(exc)[:200])
        return finish(None)
    evidence['floors'] = floors

    # The pass's command value flips the point's served baseline —
    # every pass's write actually moves the image, so the
    # gated-image clause discriminates whether the pending write
    # ever landed on the standby.
    baseline = _point_value(_try_snapshot(ctx, base) or {}, point)
    value = not baseline if isinstance(baseline, bool) else True

    # The staged adoption: the receipted submission lands pending
    # on the owner until its next scan applies it; the driven
    # peer's pull — frozen outside POST /scan — must land inside
    # that window, so the submit follows the owner's scan boundary
    # and one driven scan pulls the still-Accepted receipt. A pull
    # that lands past the apply boundary adopts the line's settled
    # record instead — a miss that restages — while a settled
    # verdict with no line record behind it is the phantom minted
    # on the gated image, the defect the audit exists to name.
    command = {'write_value': {'point': point, 'kind': 'bool',
                               'value': {'bool': value}}}
    staged = None
    staged_class = None
    attempts = []
    for attempt in range(QUIESCED_ATTEMPTS):
        candidate = {'command': command, 'point': point,
                     'value': value,
                     'actor': 'qa-quiesced-' + str(number)
                              + '-' + str(attempt),
                     'reason': 'quiesced-standby-settle-'
                               + str(number) + '-' + str(attempt)}
        tick = (_try_snapshot(ctx, base) or {}).get('tick') or 0
        edge = wait_for(
            lambda t=tick: (
                (_try_snapshot(ctx, base) or {}).get('tick') or 0)
            > t or None,
            time.monotonic() + QUIESCED_EDGE, interval=0.02)
        try:
            status, body = http_json(
                'POST', base + '/command',
                {'command': command, 'actor': candidate['actor'],
                 'reason': candidate['reason']})
            _drive(ctx, 1)
        except Exception as exc:
            status, body = None, str(exc)
        adopted = _standing_receipt(ctx, driven_base, candidate)
        entry = {'edge': bool(edge), 'status': status,
                 'receipt': body, 'adopted': adopted}
        attempts.append(entry)
        if status != 200 or _outcome_key(body) != 'accepted':
            entry['class'] = 'unadmitted'
            continue
        if adopted is not None \
                and _outcome_key(adopted) == 'accepted':
            entry['class'] = 'pending'
            staged, staged_class = candidate, 'pending'
            break
        if adopted is not None:
            owner_served = _standing_receipt(ctx, base, candidate)
            covered = _applied_tick(adopted) is not None \
                and _applied_tick(adopted) \
                == _applied_tick(owner_served)
            entry['class'] = 'covered' if covered else 'phantom'
            if not covered:
                staged, staged_class = candidate, 'phantom'
                break
    evidence['staging'] = attempts
    if staged is None:
        return inconclusive(
            'window', 'the driven peer\'s pull never landed inside '
            'the pending window — a still-Accepted adoption never '
            'staged in ' + str(QUIESCED_ATTEMPTS)
            + ' attempts; the rig presents no quiesced-adoption '
            'window to audit')
    admission = staged
    evidence['admission'] = admission
    digest['adopted'] = staged_class

    # The quiesced hold: the standby carries the still-pending
    # receipt while the pair stays settled and tracking — its
    # serving monitor must show no terminal verdict, its journal
    # and durable file no command_settled, and its gated image the
    # pre-command baseline.
    held = {'receipt': _standing_receipt(ctx, driven_base,
                                         admission),
            'journaled': _standing_settles(ctx, driven_base,
                                           floors['driven'],
                                           admission),
            'image': _point_value(
                _try_snapshot(ctx, driven_base) or {}, point),
            'pair': (_pair_active(ctx) == owner
                     and _tracking_standby(ctx, peer) is not None)}
    try:
        held['durable'] = _durable_settles(
            journal_files['driven'], admission)
    except Exception as exc:
        failed('durable', 'the driven peer\'s journal file never '
               'read: ' + str(exc)[:200])
        held['durable'] = []
    evidence['held'] = held
    digest['gated'] = _quiesced_pending(held, admission, note)
    if not held['pair']:
        failed('pair', 'the deployed pair left its settled '
               'tracking layout while the standby held the '
               'carried command')

    # The true boundary: the owner's own scan applies the
    # admission — its journaled settle is the line record every
    # adopted resolution must carry verbatim.
    line = None
    deadline = time.monotonic() + QUIESCED_AUDIT
    while line is None and time.monotonic() < deadline:
        settles = _standing_settles(ctx, base, floors[owner],
                                    admission)
        if settles:
            line = settles[-1]
        else:
            time.sleep(QUIESCED_POLL)
    evidence['line'] = line
    if line is None:
        failed('line', 'the field owner never journaled the '
               'admission\'s own boundary — the carried command '
               'has no true boundary to resolve at')
        return finish(digest)
    if _outcome_key(line) != 'applied':
        failed('line', 'the field owner settled the carried '
               'admission ' + _outcome_key(line)
               + ' — the staging expected the line\'s apply')
        return finish(digest)

    # The promote boundary: the quiesced standby's final-sync pull
    # carries the line's record across and the carried command
    # resolves exactly once — the adopted settle, never a fresh
    # mint.
    promote_status, promoted = _settle_call(driven_base
                                            + '/promote')
    evidence['promote'] = {'status': promote_status,
                           'body': promoted}
    if promote_status != 200:
        failed('promote', 'POST /promote on the quiesced standby '
               'answered ' + str(promote_status) + ' '
               + json.dumps(promoted)[:200])
        return finish(digest)
    owned = None
    deadline = time.monotonic() + QUIESCED_SETTLE
    while owned is None and time.monotonic() < deadline:
        try:
            _drive(ctx, 2)
        except Exception:
            pass
        report = _try_role(ctx, driven_base)
        if (report or {}).get('role') == 'active':
            owned = report
        else:
            time.sleep(QUIESCED_POLL)
    evidence['driven_owned'] = owned
    if owned is None:
        failed('promote-active', 'the promoted standby never '
               'settled into the active role')
        return finish(digest)

    # The audit window: every peer's journaled resolution —
    # over-journaling is terminal, no wait heals it; the expected
    # shape is exactly one applied per peer at the line's tick.
    def covered(snapshot):
        if snapshot is None:
            return None
        journaled = snapshot['journaled']
        counts = [len(journaled.get(name) or [])
                  for name in (owner, peer, 'driven')]
        if any(count > 1 for count in counts):
            return snapshot
        return snapshot if all(count == 1 for count in counts) \
            else None

    deadline = time.monotonic() + QUIESCED_AUDIT
    window = None
    while time.monotonic() < deadline:
        window = covered(
            _quiesced_window(ctx, admission, floors)) or window
        if window is not None:
            break
        time.sleep(QUIESCED_POLL)
    if window is None:
        window = _quiesced_window(ctx, admission, floors)
    evidence['window'] = window
    if window is None:
        failed('window', 'the audit\'s window never read — a '
               'serving monitor dropped its journal or receipts '
               'inside the bound')
        return finish(digest)
    digest['boundary'] = _quiesced_boundary(
        window['journaled'], window['logged'], window['images'],
        line, admission, note)

    # The durable half: the run's journal files carry the same one
    # applied settle per peer the serving monitors show — the
    # standby's file carries nothing ahead of the boundary and
    # exactly the adopted record after it.
    durable = {}
    for name in (owner, peer, 'driven'):
        path = journal_files.get(name)
        if path is None:
            continue
        try:
            durable[name] = _durable_settles(path, admission)
        except Exception as exc:
            failed('durable-' + name, name + '\'s journal file '
                   'never read: ' + str(exc)[:200])
    evidence['durable'] = {
        name: [_outcome_key(receipt) for receipt in entries]
        for name, entries in durable.items()}
    for name, entries in durable.items():
        if len(entries) != 1:
            note('durable-' + name,
                 'quiesced-settle-failed' if not entries
                 else 'quiesced-settle-nondeterministic',
                 name + '\'s durable journal carries '
                 + str(len(entries))
                 + ' command_settled records for the carried '
                 'admission — the boundary owes exactly one there')
            digest['boundary'] = 'diverged'
            continue
        receipt = entries[0]
        if _outcome_key(receipt) != 'applied' \
                or (_applied_tick(line) is not None
                    and _applied_tick(receipt)
                    != _applied_tick(line)):
            note('durable-' + name,
                 'quiesced-settle-nondeterministic',
                 name + '\'s durable journal settles the carried '
                 'admission ' + _outcome_key(receipt) + ' at tick '
                 + str(_applied_tick(receipt))
                 + ' — the line\'s record applied it at '
                 + str(_applied_tick(line)))
            digest['boundary'] = 'diverged'

    # Restore the launch roles: demote the promoted standby onto
    # its configured --standby source, re-promote the entry owner,
    # and reconverge every peer.
    restore_status, restore = _settle_call(driven_base + '/demote')
    evidence['restore_demote'] = {'status': restore_status,
                                  'body': restore}
    if restore_status != 200:
        failed('restore', 'POST /demote on the promoted standby '
               'answered ' + str(restore_status) + ' '
               + json.dumps(restore)[:200])
        return finish(digest)
    promoted_back, last = None, None
    deadline = time.monotonic() + QUIESCED_SETTLE
    while promoted_back is None and time.monotonic() < deadline:
        status, body = _settle_call(base + '/promote')
        if status == 200:
            promoted_back = body
        else:
            last = (status, body)
            time.sleep(QUIESCED_POLL)
    evidence['restore_promote'] = {'body': promoted_back,
                                   'last_refusal': last}
    if promoted_back is None:
        failed('restore', 'the entry owner\'s promote never '
               'succeeded: ' + json.dumps(last)[:300])
        return finish(digest)
    restored = None
    deadline = time.monotonic() + QUIESCED_SETTLE
    while restored is None and time.monotonic() < deadline:
        try:
            _drive(ctx, 2)
        except Exception:
            pass
        reports = {name: _try_role(ctx, ctx[name])
                   for name in (owner, peer, 'driven')}
        if (reports[owner] or {}).get('role') == 'active' \
                and _tracking(reports[peer]) \
                and _tracking(reports['driven']):
            restored = reports
        else:
            time.sleep(QUIESCED_POLL)
    evidence['restored'] = restored
    if restored is None:
        failed('restore', 'the rig never settled back to the '
               'entry role layout: ' + json.dumps(reports)[:300])
        return finish(digest)
    digest['roles'] = 'restored'
    return finish(digest)


def scenario_quiesced_standby_settle(ctx):
    """Exercise the #689 quiesced-standby no-phantom-settle
    contract on the deployed pair: converge the driven third
    controller behind the field owner, submit a receipted
    writable-point command whose pull the driven standby lands
    inside the pending window, and audit through its serving
    monitor and durable journal that the quiesced scans mint no
    settle on the gated image — no terminal verdict, no
    command_settled, the staged image unchanged — then promote and
    assert the carried command resolves exactly once at the
    boundary on every peer, at the line's own apply tick."""
    case = Case(
        'quiesced-standby-settle',
        'Quiesced standby settles no adopted pending command',
        'with the deployed pair settled and tracking, a receipted '
        'writable-point command on the active is adopted still '
        'pending by a tracking standby held quiesced — the '
        'standby\'s serving monitor carries no terminal receipt '
        'and its monitor and durable journal no command_settled '
        'ahead of the boundary, the gated image unchanged — then '
        'the promoted standby resolves the carried command '
        'exactly once at the true boundary, the adopted record '
        'carrying the line\'s applied verdict on every peer, and '
        'the pair\'s launch roles restore; two passes produce '
        'identical digests')
    owner = peer = None
    try:
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries only one endpoint — the '
                               'pair the quiesced audit needs is '
                               'absent')
        for action in ('start_driven', 'stop_driven'):
            if ctx.get(action) is None:
                return case.finish('inconclusive', 'the run '
                                   'context carries no ' + action
                                   + ' action — the rig seam the '
                                   'quiesced staging needs is '
                                   'absent')
        if ctx.get('driven') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no driven endpoint — the '
                               'quiesced standby has no monitor')
        journal_files = ctx.get('journal_files') or {}
        missing = [name for name in ('active', 'standby', 'driven')
                   if journal_files.get(name) is None]
        if missing:
            return case.finish('inconclusive', 'the run context '
                               'carries no journal files for '
                               + json.dumps(missing) + ' — the '
                               'durable half of the audit is '
                               'absent')
        for name in ('active', 'standby'):
            try:
                _role(ctx, ctx[name])
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s '
                                   'monitor is unreachable: '
                                   + str(exc)[:200])
        deadline = time.monotonic() + QUIESCED_SETTLE
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=QUIESCED_POLL)
        if owner is None:
            return case.finish('failed', 'no peer reports '
                               'role=active')
        peer = 'standby' if owner == 'active' else 'active'
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=QUIESCED_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the tracking '
                               'window the leg holds the pair '
                               'across was never reached')
        # The contract surface: the owner's checkpoint must carry
        # the receipt window and admission counters the index
        # correlation reads, and the command path must echo the
        # declared actor/reason — the submission-record identity
        # the audit compares.
        try:
            _, checkpoint = http_json('GET', ctx[owner]
                                      + '/checkpoint')
        except Exception as exc:
            return case.finish('inconclusive', 'the field owner\'s '
                               'checkpoint never answered: '
                               + str(exc)[:200])
        if not isinstance(checkpoint, dict) \
                or not isinstance(checkpoint.get('receipts'), list) \
                or not isinstance(
                    (checkpoint.get('command_admission') or {})
                    .get('attempts'), int):
            return case.finish('inconclusive', 'the served '
                               'checkpoint carries no receipt '
                               'window or admission counters — '
                               'the rig predates the '
                               'quiesced-standby-settle contract')
        _, signals = http_json('GET', ctx[owner] + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'quiesced-settle-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming the '
                      'writable command point')
        points = _writable_bool_points(signals, 1)
        if not points:
            return case.finish('inconclusive', 'the model declares '
                               'no writable bool command point')
        point = points[0]
        snapshot = _snapshot(ctx, ctx[owner])
        baseline = _point_value(snapshot, point)
        if not isinstance(baseline, bool):
            baseline = False
        # The attribution probe: a receipted admission echoing its
        # declared actor and reason — the submission-record
        # identity the audit compares — before any staging.
        status, probe = http_json(
            'POST', ctx[owner] + '/command',
            {'command': {'write_value': {
                'point': point, 'kind': 'bool',
                'value': {'bool': baseline}}},
             'actor': 'qa-quiesced-contract-probe',
             'reason': 'quiesced-standby-settle-contract'})
        if status != 200 or _outcome_key(probe) != 'accepted':
            return case.finish('inconclusive', 'the contract probe '
                               'submission drew no admission: '
                               + str(status) + ' '
                               + json.dumps(probe)[:200])
        if probe.get('actor') != 'qa-quiesced-contract-probe' \
                or probe.get('reason') \
                != 'quiesced-standby-settle-contract':
            return case.finish('inconclusive', 'the served receipt '
                               'drops the declared actor/reason — '
                               'the rig predates the '
                               'quiesced-standby-settle contract')
        case.observe('field owner: ' + owner + ' (' + ctx[owner]
                     + '); sibling standby: ' + peer
                     + '; carried point ' + str(point))
        try:
            launched = ctx['start_driven'](owner)
        except Exception as exc:
            return case.finish('inconclusive', 'the driven-peer '
                               'launch never completed: '
                               + str(exc)[:300])
        case.observe('driven peer up on ' + owner + ': '
                     + str((launched or {}).get('container')))
        if wait_for(lambda: _try_role(ctx, ctx['driven']),
                    time.monotonic() + QUIESCED_SETTLE,
                    interval=QUIESCED_POLL) is None:
            return case.finish('inconclusive', 'the driven monitor '
                               'never answered /role')
        digests = []
        audits = []
        try:
            for number in (1, 2):
                digest, violations, evidence = _quiesced_pass(
                    ctx, number, owner, peer, point)
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'quiesced-settle-pass-' + str(number) + '.json',
                    evidence)
                case.evidence('file', ref, 'quiesced-standby pass '
                              + str(number) + ' — the staged '
                              'pending adoption, the quiesced '
                              'window\'s monitor and durable '
                              'audits, the boundary\'s '
                              'exactly-once resolution, the role '
                              'restore, and the normalized digest')
                if evidence.get('inconclusive'):
                    return case.finish(
                        'inconclusive',
                        evidence['inconclusive'])
                if violations or digest is None:
                    diagnostic = 'quiesced-settle-failed' \
                        if digest is None or any(
                            name == 'quiesced-settle-failed'
                            for name, _
                            in violations.values()) \
                        else 'quiesced-settle-nondeterministic'
                    return case.finish(
                        'failed', diagnostic + ': ' + '; '.join(
                            detail for _, detail in
                            list(violations.values())[:4]))
                digests.append(digest)
                audits.append(evidence)
        finally:
            # The launch layout for the cases behind this one: a
            # clean pass restores it by construction; an aborted
            # pass may have left the driven peer owning the field —
            # demote whichever peer still owns it, promote the
            # entry owner, and converge — then the driven container
            # is removed.
            try:
                restored = False
                deadline = time.monotonic() + QUIESCED_SETTLE
                while not restored \
                        and time.monotonic() < deadline:
                    try:
                        _drive(ctx, 2)
                    except Exception:
                        pass
                    reports = {name: _try_role(ctx, ctx[name])
                               for name in (owner, peer, 'driven')}
                    for name, report in reports.items():
                        if name != owner \
                                and (report or {}).get('role') \
                                == 'active':
                            _settle_call(ctx[name] + '/demote')
                    if (reports[owner] or {}).get('role') \
                            == 'standby':
                        _settle_call(ctx[owner] + '/promote')
                    restored = _pair_active(ctx) == owner \
                        and _tracking_standby(ctx, peer) is not None
                    if not restored:
                        time.sleep(QUIESCED_POLL)
            except Exception as exc:
                case.observe('cleanup: role restore failed: '
                             + str(exc)[:200])
            try:
                ctx['stop_driven']()
            except Exception:
                pass
        if digests[0] != digests[1]:
            return case.finish(
                'failed', 'quiesced-settle-nondeterministic: the '
                'two passes\' digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two quiesced-standby passes, identical '
                     'digests')

        # The unchecked self-check: each audit clause, run over a
        # planted negative, must name its violation — a phantom
        # settle minted on the gated image and a carried command
        # the boundary never resolved — a silent audit can no
        # longer be trusted to catch what it names.
        admission = audits[-1].get('admission')
        line = audits[-1].get('line')
        if admission is not None:
            phantom_receipt = {
                'command': admission['command'],
                'actor': admission['actor'],
                'outcome': {'applied': {'tick': 0}}}
            planted = {}
            verdict = _quiesced_pending(
                {'receipt': phantom_receipt,
                 'journaled': [phantom_receipt],
                 'durable': [phantom_receipt],
                 'image': admission['value']},
                admission,
                lambda key, diagnostic, detail:
                    planted.setdefault(key, (diagnostic, detail)))
            if verdict != 'phantom' \
                    or not any(name == 'quiesced-settle-failed'
                               for name, _ in planted.values()):
                return case.finish(
                    'failed', 'quiesced-settle-unchecked: the '
                    'pending-window audit stayed silent on a '
                    'planted phantom settle')
            planted = {}
            _quiesced_boundary(
                {name: [] for name in (owner, peer, 'driven')},
                {name: [] for name in (owner, peer, 'driven')},
                {}, line, admission,
                lambda key, diagnostic, detail:
                    planted.setdefault(key, (diagnostic, detail)))
            if not any(name == 'quiesced-settle-failed'
                       for name, _ in planted.values()):
                return case.finish(
                    'failed', 'quiesced-settle-unchecked: the '
                    'boundary audit stayed silent on a planted '
                    'missing resolution')
            case.observe('the self-check leg\'s planted negatives '
                         'each reported their named diagnostic')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
