"""The repromote_suspended_settle acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the re-promote settle leg shares the launch-layout
# window behind the demote legs — it needs the settled tracking
# pair, the field claim the preempting promote rides, and the
# durable journals, and it restores the launch roles before the
# tune case's a->b switch.
RUNS_BEFORE = frozenset({'scenario_parameter_tune_carryover'})


# --------------------------------------------------------------------
# The re-promoted holder's suspended-receipt settle contract — the
# lane evidence for #1050's fix serving WW-LCM-001's continuity
# clause and the receipt-as-truth rule. A receipted command a
# demoting holder suspends — the `Accepted` entry the fenced
# boundary parks for the surviving line to adjudicate — resolves on
# three paths the architecture names: a covering adoption on the
# tracked stream, a promotion-boundary carry, or — the duty this leg
# exercises — the same holder's own re-promotion inside the run,
# whose first field-owning scan re-queues every still-`Accepted`
# receipt it owns but does not already carry. The defect #1050
# closed: a same-run demote->promote parked the receipt `Accepted`
# forever on the live active — invisible to /receipts' terminal
# verdict — until a later successor's promotion-boundary carry
# applied the stale command out from under the line's settled state.
#
# Staging the uncovered suspended tail deterministically takes the
# misordered promote: a tracking sibling's promotion boundary runs
# its final-sync fetch against the holder it tracks, and any
# admission the holder already logged carries into the promoted
# run — the carry path, not this contract. The leg therefore
# promotes the sibling FIRST: its boundary fetch meets the
# pre-admission document, its claim preempts the incumbent, and —
# `owns_field` covering the promoting state — it never pulls the
# holder's checkpoint again, so its served window can never cover
# the admission the leg next lands. The submission then races the
# incumbent's fenced-but-still-reporting window: the owner admits
# the command, its detection scan suspends the pending receipt to
# `Accepted` and demotes in place, and the demoted peer resolves the
# promoted sibling through the field's declared claim monitor — the
# reconvergence to a promotable verdict on the promoted peer's
# checkpoint stream the issue names. The adopted window's high-water
# never reaches the admission's index, so the receipt stays
# suspended as the demoted log's unreached tail — the restored
# suspended receipt the re-promote owes a verdict.
#
# The re-promoted holder's first field-owning scan re-queues the
# suspended tail and settles it `applied` exactly once — the
# admission's own boundary, never a parked `Accepted` on the live
# active and never a deferred apply on a later successor's
# promotion. The audit reads both serving monitors and both durable
# journals: the point serves the command's value, /receipts carries
# the terminal verdict rather than a parked accepted, and one
# command_settled per admission stands on each peer's journal — the
# settler's own boundary plus the adopted record the demoted sibling
# journals reconverging.
#
# Two consecutive passes must produce identical digests; the
# self-check leg plants a doctored settled-without-apply record, a
# doubled settle, and a parked-accepted log through the audit — a
# silent audit reports repromote-suspended-settle-unchecked.
# Inconclusive when the staged run predates the contract — no
# receipt-level actor/reason attribution, no checkpoint receipt
# window or admission counters — or cannot reach the rig it names.

REPROMOTE_SETTLE = 45    # bound on each demote, reconverge, promote,
                         # and role restore inside the leg
REPROMOTE_AUDIT = 30     # bound on the restored receipt's settle
                         # journaling on both peers
REPROMOTE_POLL = 0.4     # wait cadence inside the leg
REPROMOTE_WATCH = 0.05   # cadence polling the demoting holder —
                         # the demoting walk is one scan
REPROMOTE_ATTEMPTS = 3   # fenced-window submission bound — a
                         # submission that lands after the detection
                         # scan restages on the restored roles


def _tracking(report):
    """Whether a /role report shows a tracking standby — the
    promotable verdict the demoted peer's pulls on the promoted
    peer's stream converge to."""
    return (report or {}).get('role') == 'standby' \
        and 'tracking' in ((report or {}).get('sync') or {})


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
    """The monitor's served receipt for the admission — matched on
    the (command, actor) submission identity — or None."""
    try:
        receipts, _ = _receipt_window(ctx, base)
    except Exception:
        return None
    return next((entry for entry in receipts
                 if _admission_hit(entry, admission)), None)


def _restore_launch_roles(ctx, owner, peer, deadline):
    """Best-effort launch-layout restore inside one bound: demote
    whichever peer holds the field off the launch owner, promote the
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
        time.sleep(REPROMOTE_POLL)
    return _pair_active(ctx) == owner \
        and _tracking_standby(ctx, peer) is not None


def _repromote_window(ctx, admission, floors):
    """One polled audit snapshot for the suspended admission: each
    peer's journaled command_settled receipts, served receipt-log
    matches, and the image value at the admission's point, since the
    pass's journal floors. None while either peer's monitor drops a
    read — a lost observation, never the audit's verdict."""
    window = {'journaled': {}, 'logged': {}, 'image': {}}
    for name in ('active', 'standby'):
        try:
            _, journal = http_json('GET', ctx[name] + '/journal?since='
                                   + str(floors[name]))
            _, receipts = http_json('GET', ctx[name] + '/receipts')
            snapshot = _snapshot(ctx, ctx[name])
        except Exception:
            return None
        window['journaled'][name] = [
            receipt for receipt in
            (_journal_settled(entry) for entry in _journal_list(journal))
            if _admission_hit(receipt, admission)]
        window['logged'][name] = [
            receipt for receipt in _receipt_list(receipts)
            if _admission_hit(receipt, admission)]
        window['image'][name] = _point_value(snapshot,
                                             admission['point'])
    return window


def _repromote_resolved(window, admission):
    """Whether the audit snapshot is terminal for the restored
    admission: both peers journaled the applied settle and both
    adopted logs agree — or a contradiction already showing, which
    no further wait can heal."""
    demoted, promoted = admission['demoted'], admission['promoted']
    counts = {name: len(entries)
              for name, entries in window['journaled'].items()}
    outcomes = {_outcome_key(receipt)
                for entries in window['journaled'].values()
                for receipt in entries}
    if len(outcomes) > 1:
        return True           # divergent verdicts — terminal
    if outcomes and outcomes != {'applied'}:
        return True           # an out-of-contract outcome is terminal
    if any(_outcome_key(receipt) not in ('applied', 'accepted')
           for entries in window['logged'].values()
           for receipt in entries):
        return True           # the adopted logs already show an
                              # out-of-contract verdict
    if outcomes != {'applied'}:
        return False
    return counts.get(demoted) == 1 and counts.get(promoted) == 1 \
        and len(window['logged'][demoted]) == 1 \
        and len(window['logged'][promoted]) == 1 \
        and all(_outcome_key(receipt) == 'applied'
                for name in (demoted, promoted)
                for receipt in window['logged'][name])


def _repromote_judge(window, durable, admission, note):
    """The restored admission's audit clauses over one resolved
    window plus the durable journal counts — records a violation for
    every clause the evidence breaks and answers 'single' or
    'diverged' for the digest. `window` is _repromote_window's
    journaled/logged/image record; `durable` maps each peer key to
    its --journal-file's matching command_settled receipts (or None
    when the file never read)."""
    demoted, promoted = admission['demoted'], admission['promoted']
    label = 'the restored admission ' + str(admission['actor']) \
        + ' (index ' + str(admission['index']) + ', point ' \
        + str(admission['point']) + ')'
    verdict = 'single'
    journaled, logged = window['journaled'], window['logged']
    # The parked-accepted check first — the defect this leg exists
    # to name: the restored receipt still Accepted on a live peer
    # past the re-promoted boundary.
    for name in (demoted, promoted):
        if any(_outcome_key(receipt) == 'accepted'
               for receipt in logged[name]):
            verdict = 'diverged'
            note('parked-' + name,
                 'repromote-suspended-settle-failed',
                 label + ' is still parked Accepted on ' + name
                 + ' — the re-promoted holder\'s boundary never '
                 're-queued it')
    outcomes = {_outcome_key(receipt)
                for entries in journaled.values()
                for receipt in entries}
    if not outcomes:
        verdict = 'diverged'
        note('settle', 'repromote-suspended-settle-failed',
             label + ' journaled no command_settled on either peer '
             '— the re-promoted boundary never settled it')
        outcome = None
    elif len(outcomes) != 1:
        verdict = 'diverged'
        note('outcomes',
             'repromote-suspended-settle-nondeterministic',
             label + ' journaled ' + json.dumps(sorted(outcomes))
             + ' — one admission, never more than one terminal '
             'outcome')
        outcome = None
    else:
        outcome = next(iter(outcomes))
        if outcome != 'applied':
            verdict = 'diverged'
            note('outcome',
                 'repromote-suspended-settle-nondeterministic',
                 label + ' settled ' + outcome + ' — the restored '
                 'suspended receipt owes applied')
    # The journal coverage: the re-promoted holder journals the
    # admission's own settle once; the demoted sibling journals the
    # adopted record once — exactly one command_settled per peer.
    for name in (demoted, promoted):
        count = len(journaled[name])
        if count == 1:
            continue
        verdict = 'diverged'
        diagnostic = 'repromote-suspended-settle-failed' \
            if count == 0 \
            else 'repromote-suspended-settle-nondeterministic'
        note('journal-' + name, diagnostic,
             label + ' journaled ' + str(count)
             + ' command_settled records on ' + name
             + ' — the contract owes exactly one there: the '
             + ('re-promoted holder\'s own boundary'
                if name == demoted
                else 'adopted record on the demoted sibling'))
    # The adopted logs: each peer's served log carries the
    # admission's receipt settled applied — the terminal verdict,
    # never a parked accepted and never a diverging record.
    for name in (demoted, promoted):
        receipts = logged[name]
        if len(receipts) > 1:
            verdict = 'diverged'
            note('log-count-' + name,
                 'repromote-suspended-settle-nondeterministic',
                 name + "'s served log carries " + str(len(receipts))
                 + ' receipts for ' + label)
        for receipt in receipts:
            served = _outcome_key(receipt)
            if served != 'applied' and served != 'accepted':
                verdict = 'diverged'
                note('log-' + name,
                     'repromote-suspended-settle-nondeterministic',
                     name + "'s served log carries " + served
                     + ' for ' + label
                     + ' where the journal settled applied')
        if outcome == 'applied' and not receipts:
            verdict = 'diverged'
            note('log-missing-' + name,
                 'repromote-suspended-settle-nondeterministic',
                 name + "'s served log lost " + label
                 + ' the journal settled applied')
    # The durable half: each peer's --journal-file carries the same
    # one settle the serving monitor shows.
    for name in (demoted, promoted):
        settles = durable.get(name)
        count = None if settles is None else len(settles)
        if count != 1:
            verdict = 'diverged'
            diagnostic = 'repromote-suspended-settle-failed' \
                if count == 0 \
                else 'repromote-suspended-settle-nondeterministic'
            note('durable-' + name, diagnostic,
                 name + "'s durable journal carries " + str(count)
                 + ' command_settled records for ' + label
                 + ' — the durable record owes exactly one there')
        elif _outcome_key(settles[0]) != 'applied':
            verdict = 'diverged'
            note('durable-' + name,
                 'repromote-suspended-settle-nondeterministic',
                 name + "'s durable journal settles " + label + ' '
                 + _outcome_key(settles[0])
                 + ' — the line\'s one settlement is applied')
    # The image: the applied write present on both peers — a settle
    # whose command never applied is the doctored shape the leg
    # names.
    for name in (demoted, promoted):
        value = window['image'][name]
        if value != admission['value']:
            verdict = 'diverged'
            note('image-' + name,
                 'repromote-suspended-settle-failed',
                 label + ' settled but ' + name + "'s image serves "
                 + json.dumps(value) + ' — a settlement without the '
                 'command\'s application')
    return verdict


def _file_settles(path, admission):
    """The durable journal file's `command_settled` receipts
    matching the admission, or None while the file cannot be read —
    the durable half of the one-settle audit."""
    try:
        records = _journal_entries(path)
    except Exception:
        return None
    return [receipt for receipt in
            (_journal_settled(item) for item in records)
            if _admission_hit(receipt, admission)]


def _repromote_pass(ctx, number, owner, peer, point, value):
    """One re-promote settle pass: promote the sibling so its
    pre-admission window can never cover the coming command, land
    the receipted submission inside the holder's fenced-reporting
    window so the detection scan suspends it Accepted, let the
    demoted peer reconverge to a promotable verdict on the promoted
    peer's checkpoint stream, re-promote the original holder, then
    audit through both serving monitors and both durable journals
    that the restored suspended receipt settles exactly once at the
    re-taken boundary. Returns (digest, violations, evidence): the
    digest is the pass's normalized verdict record, identical across
    clean passes."""
    violations = {}
    evidence = {'pass': number, 'entry_owner': owner,
                'point': point}
    digest = {'preempt': 'unstaged', 'suspended': 'unstaged',
              'reconverged': 'no', 'window': 'unseen',
              'repromoted': 'no', 'settle': 'unseen',
              'image': 'unseen', 'journals': 'unread',
              'roles': 'unrestored'}
    owner_base, peer_base = ctx[owner], ctx[peer]
    journal_files = ctx.get('journal_files') or {}

    def note(key, diagnostic, detail):
        violations.setdefault(key, (diagnostic, detail))

    def failed(key, detail):
        note(key, 'repromote-suspended-settle-failed', detail)

    def finish(result):
        evidence['digest'] = result
        evidence['violations'] = {key: {'diagnostic': name,
                                        'detail': detail}
                                  for key, (name, detail)
                                  in violations.items()}
        return result, violations, evidence

    def inconclusive(key, detail):
        evidence['inconclusive'] = key + ': ' + detail
        return finish(None)

    # The settled gate: the launch owner holds the field and the
    # sibling tracks it — the layout the pass's restore owes.
    if _pair_active(ctx) != owner \
            or _tracking_standby(ctx, peer) is None:
        failed('settle', 'the pair never settled — ' + owner
               + ' holds no active role with ' + peer
               + ' tracking behind it')
        return finish(None)

    # The journal floors the served audit reads from — a peer that
    # cannot serve its journal cannot evidence the contract.
    floors = {}
    try:
        for name in (owner, peer):
            floors[name] = _journal_cursor(ctx, ctx[name])
    except Exception as exc:
        failed('floors', 'a peer\'s journal floor never served: '
               + str(exc)[:200])
        return finish(None)
    evidence['floors'] = floors

    command = {'write_value': {'point': point, 'kind': 'bool',
                               'value': {'bool': value}}}

    # ---- stage the suspended admission -----------------------------
    #
    # The preempting promote lands before the admission so the
    # sibling's final-sync fetch meets only the pre-admission
    # document — its window's high-water can never cover the
    # receipt. The submission then races the incumbent's
    # fenced-but-still-reporting window: admitted, suspended at the
    # detection scan's fenced demote. A submission refused inside
    # the closing window restages on the restored roles.
    staged = None
    attempts = []
    for round_index in range(REPROMOTE_ATTEMPTS):
        actor = 'qa-repromote-' + str(number) + '-' \
            + str(round_index)
        admission = {'actor': actor, 'command': command,
                     'point': point, 'value': value,
                     'index': _next_receipt_index(ctx, owner_base),
                     'demoted': owner, 'promoted': peer}
        attempt = {'actor': actor, 'index': admission['index']}
        # The preemption the suspension window hangs on: the
        # sibling's promote claims the field under the standing
        # holder — unconditional for a tracking peer — while its
        # boundary's final-sync pull meets only the pre-admission
        # checkpoint.
        status, body = _settle_call(peer_base + '/promote')
        attempt['promote'] = {'status': status, 'body': body}
        if status != 200:
            failed('preempt-promote', 'POST /promote on ' + peer
                   + ' answered ' + str(status) + ' '
                   + json.dumps(body)[:200])
            attempts.append(attempt)
            evidence['attempts'] = attempts
            return finish(digest)
        digest['preempt'] = 'claimed'
        # The suspended admission: fired into the holder's
        # fenced-but-still-reporting window — the detection scan
        # demotes it inside the receipt's suspension window.
        try:
            status, receipt = http_json(
                'POST', owner_base + '/command',
                {'command': command, 'actor': actor,
                 'reason': 'repromote-suspended-settle'})
        except Exception as exc:
            status, receipt = None, str(exc)
        attempt['submission'] = {'status': status,
                                 'receipt': receipt}
        landed = status == 200 \
            and _outcome_key(receipt) == 'accepted'
        if landed:
            if receipt.get('actor') != actor \
                    or receipt.get('reason') \
                    != 'repromote-suspended-settle':
                attempts.append(attempt)
                evidence['attempts'] = attempts
                return inconclusive(
                    'attribution', 'the served receipt drops the '
                    'declared actor/reason — the submission-record '
                    'identity the settle audit correlates by — the '
                    'rig predates the repromote-suspended-settle '
                    'contract')
        else:
            # The window closed first: the detection scan demoted
            # the holder before the submission landed — no
            # suspended admission ever staged. Restore the launch
            # roles and re-run the stage with a fresh identity.
            attempt['missed'] = 'window closed'
            restored = _restore_launch_roles(
                ctx, owner, peer,
                time.monotonic() + REPROMOTE_SETTLE)
            attempt['restored'] = restored
            attempts.append(attempt)
            if not restored:
                evidence['attempts'] = attempts
                return inconclusive(
                    'restage', 'the pair never re-took the launch '
                    'roles after the missed suspension window')
            continue
        attempts.append(attempt)
        # The demote watch: the fenced detection scan demotes the
        # holder in place, its pending admission suspended Accepted.
        demoted_report = None
        deadline = time.monotonic() + REPROMOTE_SETTLE
        while demoted_report is None \
                and time.monotonic() < deadline:
            report = _try_role(ctx, owner_base)
            if (report or {}).get('role') == 'standby':
                demoted_report = report
            else:
                time.sleep(REPROMOTE_WATCH)
        evidence['demoted'] = demoted_report
        if demoted_report is None:
            failed('demote', 'the preempted holder never demoted — '
                   'its reported role never walked to standby')
            return finish(digest)
        staged = admission
        break
    evidence['attempts'] = attempts
    if staged is None:
        return inconclusive(
            'suspension', 'no admission froze suspended across '
            + str(REPROMOTE_ATTEMPTS)
            + ' staged rounds — every preemption landed past the '
            'reporting window')

    # The suspended standing: the demoted holder's served receipt
    # still reads Accepted — the shape the covering adoption must
    # never have reached.
    served = _served_admission(ctx, owner_base, staged)
    evidence['suspended_standing'] = served
    if served is None:
        failed('suspended-receipt', 'the staged admission\'s '
               'receipt vanished from the demoted holder\'s log '
               'outright')
        return finish(digest)
    if _outcome_key(served) != 'accepted':
        return inconclusive(
            'covered', 'the staged admission settled '
            + _outcome_key(served) + ' on the demoted holder before '
            'reconvergence — the carry path adjudicated it, not '
            'the re-promote the leg exercises')
    digest['suspended'] = 'accepted'

    # The reconvergence: the demoted peer resolves the promoted
    # sibling through the field's declared claim monitor and pulls
    # its checkpoint stream to a promotable verdict — the
    # post-reconvergence posture the re-promote needs, never the
    # gossip-window variant.
    converged = wait_for(lambda: _tracking(
                             _try_role(ctx, owner_base)),
                         time.monotonic() + REPROMOTE_SETTLE,
                         interval=REPROMOTE_POLL)
    evidence['reconverged'] = converged
    if not converged:
        failed('reconverge', 'the demoted peer never reconverged '
               'to a promotable verdict on the promoted peer\'s '
               'checkpoint stream')
        return finish(digest)
    digest['reconverged'] = 'tracking'

    # The uncovered evidence: the promoted peer's adopted window
    # still mints at the admission's index — its stream never
    # covered the receipt — and the holder's copy still parks
    # Accepted after reconverging.
    peer_index = _next_receipt_index(ctx, peer_base)
    peer_served = _served_admission(ctx, peer_base, staged)
    holder_served = _served_admission(ctx, owner_base, staged)
    evidence['pre_repromote'] = {
        'peer_next_index': peer_index, 'peer_receipt': peer_served,
        'holder_receipt': holder_served}
    if peer_served is not None or peer_index > staged['index']:
        return inconclusive(
            'covered', 'the promoted peer\'s window covered the '
            'staged admission — its final-sync carried the receipt '
            'after all: the re-promote contract never engaged')
    if holder_served is None \
            or _outcome_key(holder_served) != 'accepted':
        return inconclusive(
            'covered', 'the staged admission settled '
            + _outcome_key(holder_served)
            + ' across the reconvergence — the tracked stream '
            'adjudicated it before the re-promote')
    digest['window'] = 'uncovered'

    # The re-promote: the reconverged holder takes the field back —
    # its first field-owning scan re-queues the suspended tail.
    status, body = _settle_call(owner_base + '/promote')
    evidence['repromote'] = {'status': status, 'body': body}
    if status != 200:
        failed('repromote', 'POST /promote on the reconverged '
               'holder answered ' + str(status) + ' '
               + json.dumps(body)[:200])
        return finish(digest)
    digest['repromoted'] = 'yes'

    # ---- the audit -------------------------------------------------
    #
    # Wait out the whole expected shape — the re-promoted holder's
    # own settle plus the demoted sibling's adopted record — then
    # judge whatever the last complete read saw.
    window = None
    deadline = time.monotonic() + REPROMOTE_AUDIT
    while time.monotonic() < deadline:
        read = _repromote_window(ctx, staged, floors)
        if read is not None:
            window = read
            if _repromote_resolved(read, staged):
                break
        time.sleep(REPROMOTE_POLL)
    evidence['window'] = window
    if window is None:
        failed('window', 'the audit\'s window never read — a '
               'serving monitor dropped its journal, receipts, or '
               'snapshot inside the bound')
        return finish(digest)

    durable = {}
    for name in (owner, peer):
        path = journal_files.get(name)
        if path is None:
            durable[name] = None
            continue
        durable[name] = _file_settles(path, staged)
    evidence['durable'] = durable

    digest['settle'] = _repromote_judge(
        window, durable, staged,
        lambda key, diagnostic, detail: note(
            key + '-' + str(staged['actor']), diagnostic, detail))
    digest['image'] = 'served' if all(
        window['image'][name] == value
        for name in (owner, peer)) else 'unserved'
    digest['journals'] = 'once-each' if not any(
        key.startswith('durable-') or key.startswith('journal-')
        for key in violations) else 'split'

    # The launch roles: the re-promoted owner holds the field, the
    # demoted sibling tracks it — the layout the cases behind this
    # one enter on.
    restored = _pair_active(ctx) == owner \
        and _tracking_standby(ctx, peer) is not None
    if not restored:
        # Give the demoted sibling its reconvergence bound before
        # calling the layout unrestored.
        _restore_launch_roles(ctx, owner, peer,
                              time.monotonic() + REPROMOTE_SETTLE)
        restored = _pair_active(ctx) == owner \
            and _tracking_standby(ctx, peer) is not None
    if restored:
        digest['roles'] = 'restored'
    else:
        failed('roles', 'the pair did not land back on the launch '
               'roles — owner ' + str(_pair_active(ctx)))
    return finish(digest)


def _self_check():
    """The leg's unchecked-diagnostic self-test: run the audit judge
    over each planted negative it must name — a settle the image
    never applied, a doubled journal, a parked-accepted log — and
    require the judge to note its named violation. A silent judge
    returns the negative names it let through."""
    admission = {'actor': 'self-check', 'point': 7, 'value': True,
                 'index': 3, 'demoted': 'active',
                 'promoted': 'standby',
                 'command': {'write_value': {
                     'point': 7, 'kind': 'bool',
                     'value': {'bool': True}}}}
    settled = {'command': admission['command'], 'actor': 'self-check',
               'outcome': {'applied': {'tick': 1}}}
    parked = {'command': admission['command'], 'actor': 'self-check',
              'outcome': {'accepted': {'apply_tick': 1}}}

    def clean():
        return {'journaled': {'active': [dict(settled)],
                              'standby': [dict(settled)]},
                'logged': {'active': [dict(settled)],
                           'standby': [dict(settled)]},
                'image': {'active': True, 'standby': True}}

    durable = {'active': [dict(settled)], 'standby': [dict(settled)]}
    slipped = []

    def judge(window, files):
        found = []
        _repromote_judge(
            window, files, admission,
            lambda key, diagnostic, detail: found.append(diagnostic))
        return found

    # The planted settle-without-apply: the journal claims the
    # verdict but the image never took the write.
    window = clean()
    window['image']['active'] = False
    found = judge(window, durable)
    if 'repromote-suspended-settle-failed' not in found:
        slipped.append('settle-without-apply')
    # The planted double settle: two command_settled records for one
    # admission on its holder.
    window = clean()
    window['journaled']['active'] = [dict(settled), dict(settled)]
    found = judge(window, durable)
    if 'repromote-suspended-settle-nondeterministic' not in found:
        slipped.append('double-settle')
    # The planted parked receipt: the log still reads Accepted and
    # no journal ever settled it.
    window = clean()
    window['journaled'] = {'active': [], 'standby': []}
    window['logged']['active'] = [dict(parked)]
    found = judge(window, {'active': [], 'standby': []})
    if 'repromote-suspended-settle-failed' not in found:
        slipped.append('parked-accepted')
    return slipped


def scenario_repromote_suspended_settle(ctx):
    """Exercise the #1050 re-promoted holder's suspended-receipt
    settle contract on the deployed pair: promote the tracking
    sibling so its adopted window predates the coming admission,
    land a receipted writable-point command inside the holder's
    fenced-reporting window so the demote suspends it Accepted, let
    the demoted peer reconverge to a promotable verdict on the
    promoted peer's checkpoint stream, re-promote the original
    holder, and audit through both serving monitors and both durable
    journals that the restored suspended receipt settles exactly
    once at the re-taken boundary — applied once on each peer, the
    point serving the command's value, no parked Accepted."""
    case = Case(
        'repromote-suspended-settle',
        'A re-promoted holder settles its suspended receipt once',
        'with the deployed pair settled and tracking, promote the '
        'tracking sibling so its final-sync window predates the '
        'coming admission and its claim preempts the holder, land a '
        'receipted writable-point command inside the holder\'s '
        'fenced-reporting window so the demote suspends the receipt '
        'Accepted, let the demoted peer reconverge to a promotable '
        'verdict on the promoted peer\'s checkpoint stream — the '
        'stream that never covered the admission — then re-promote '
        'the original holder: through both serving monitors and '
        'both durable journals the restored suspended receipt '
        'settles exactly once at the re-promoted holder\'s '
        'field-owning boundary — the point serves the command\'s '
        'value, /receipts carries the terminal verdict rather than '
        'a parked accepted, one command_settled per admission '
        'stands on each peer\'s journal, and the pair\'s launch '
        'roles restore; two passes produce identical digests')
    owner, peer = 'active', 'standby'
    try:
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries only one endpoint — the '
                               'pair the re-promote leg needs is '
                               'absent')
        journal_files = ctx.get('journal_files') or {}
        missing = [name for name in (owner, peer)
                   if journal_files.get(name) is None]
        if missing:
            return case.finish('inconclusive', 'the run context '
                               'carries no journal files for '
                               + json.dumps(missing) + ' — the '
                               'durable half of the settle audit '
                               'is absent')
        for name in (owner, peer):
            try:
                _role(ctx, ctx[name])
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s '
                                   'monitor is unreachable: '
                                   + str(exc)[:200])
        deadline = time.monotonic() + REPROMOTE_SETTLE
        active = wait_for(lambda: _pair_active(ctx), deadline,
                          interval=REPROMOTE_POLL)
        if active is None:
            return case.finish('failed', 'no peer reports '
                               'role=active')
        if active != owner:
            return case.finish('inconclusive', 'the field owner is '
                               + active + ' — the re-promote '
                               'exercise needs the launched-active '
                               'peer owning the field; the pair\'s '
                               'layout predates the stage')
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=REPROMOTE_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the settle the '
                               'leg suspends inside was never '
                               'reached')
        # The contract surface: the owner's checkpoint must carry
        # the receipt window and admission counters the index
        # correlation reads, and the command path must echo the
        # declared actor/reason the admission identity rides.
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
                               'repromote-suspended-settle '
                               'contract')
        _, signals = http_json('GET', ctx[owner] + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'repromote-settle-signals.json', signals)
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
        value = not baseline
        # The attribution probe: a receipted admission echoing its
        # declared actor and reason — the submission-record
        # identity the settle audit correlates by — before any
        # induction.
        status, probe = http_json(
            'POST', ctx[owner] + '/command',
            {'command': {'write_value': {
                'point': point, 'kind': 'bool',
                'value': {'bool': baseline}}},
             'actor': 'qa-repromote-contract-probe',
             'reason': 'repromote-suspended-settle-contract'})
        if status != 200 or _outcome_key(probe) != 'accepted':
            return case.finish('inconclusive', 'the contract probe '
                               'submission drew no admission: '
                               + str(status) + ' '
                               + json.dumps(probe)[:200])
        if probe.get('actor') != 'qa-repromote-contract-probe' \
                or probe.get('reason') \
                != 'repromote-suspended-settle-contract':
            return case.finish('inconclusive', 'the served receipt '
                               'drops the declared actor/reason — '
                               'the rig predates the '
                               'repromote-suspended-settle '
                               'contract')
        case.observe('field owner: ' + owner + ' (' + ctx[owner]
                     + '); sibling standby: ' + peer
                     + '; suspended point ' + str(point))
        digests = []
        audits = []
        try:
            for number in (1, 2):
                digest, violations, evidence = _repromote_pass(
                    ctx, number, owner, peer, point, value)
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'repromote-settle-pass-' + str(number) + '.json',
                    evidence)
                case.evidence('file', ref, 're-promote settle '
                              'pass ' + str(number) + ' — the '
                              'preempting promote, the fenced '
                              'demote\'s suspended receipt, the '
                              'reconverged re-promote, the monitor '
                              'and durable journal audits, and '
                              'the normalized digest')
                if evidence.get('inconclusive'):
                    return case.finish(
                        'inconclusive',
                        evidence['inconclusive'])
                if violations or digest is None:
                    diagnostic = 'repromote-suspended-settle-' \
                        'failed' \
                        if digest is None or any(
                            name == 'repromote-suspended-settle'
                            '-failed'
                            for name, _ in violations.values()) \
                        else 'repromote-suspended-settle-' \
                            'nondeterministic'
                    return case.finish(
                        'failed', diagnostic + ': ' + '; '.join(
                            detail for _, detail in
                            list(violations.values())[:4]))
                digests.append(digest)
                audits.append(evidence)
        finally:
            # The launch layout for the cases behind this one: a
            # clean pass restores it by construction; an aborted
            # pass may have left the sibling owning the field and
            # the launch owner demoted — demote whichever peer
            # still owns the field, promote the entry owner, and
            # let the pair reconverge.
            try:
                restored = _restore_launch_roles(
                    ctx, owner, peer,
                    time.monotonic() + REPROMOTE_SETTLE)
                if not restored:
                    case.observe('cleanup: the pair did not '
                                 'settle back to the launch roles')
            except Exception as exc:
                case.observe('cleanup: role restore failed: '
                             + str(exc)[:200])
        if digests[0] != digests[1]:
            return case.finish(
                'failed',
                'repromote-suspended-settle-nondeterministic: '
                'the two passes\' digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two re-promote settle passes, identical '
                     'digests')

        # The unchecked self-check: each audit clause, run over a
        # planted negative, must name its violation — a silent
        # audit can no longer be trusted to catch what it names.
        slipped = _self_check()
        if slipped:
            return case.finish(
                'failed',
                'repromote-suspended-settle-unchecked: the '
                'settle audit stayed silent on planted negatives: '
                + ', '.join(slipped))
        case.observe('the self-check leg\'s planted negatives '
                     'each reported their named diagnostic')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
