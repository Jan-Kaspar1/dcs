"""The gossip_repromote_settle acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the gossip-window re-promote leg shares the launch-layout
# window behind the demote legs — it needs the settled tracking pair,
# the durable journals, and the writable command point, and it
# restores the launch roles before the tune case's a->b switch.
RUNS_BEFORE = frozenset({'scenario_parameter_tune_carryover'})


# --------------------------------------------------------------------
# The re-promoted holder's suspended receipt inside the gossip
# window — the lane evidence for the contract #708's fix
# establishes (WW-LCM-001's receipt-as-truth clause): a suspended
# still-`Accepted` receipt must resolve when the same holder is
# re-promoted, and the variant the post-reconvergence leg (#1109)
# scopes itself away is the one re-promoted *before any peer's
# tracking pull covers the admission*. The defect #708 named: with no
# covering adoption ever reaching the index, the entry parked
# `Accepted` forever on the live active — invisible to /receipts'
# terminal verdict, the point never written, the command queue empty
# — and the still-served receipt let a later successor resurrect the
# stale write after newer commands had settled.
#
# The staging is the finding's reproduction, on the rig's own free
# running scans: the receipted writable-point command and the
# holder's demote are issued back to back, so the demote's boundary
# suspends the still-pending receipt `Accepted` — the one-scan window
# is the whole race. The leg then polls the demoted holder's role
# until it reports a promotable verdict and promotes that same
# holder immediately, inside the gossip window: the peer that would
# otherwise carry the receipt back covered has not run its own pull
# yet. Whether the window held or a peer's pull closed it first is
# recorded as evidence, never as a verdict — both paths owe the same
# exactly-once resolution, and only the first exercises the contract,
# so the audit asserts the resolution the defect broke rather than
# which window it landed in.
#
# The audit reads both serving monitors and both durable journals: the
# point serves the command's value, /receipts carries the terminal
# verdict rather than a parked accepted, and one command_settled per
# admission stands on each peer's journal. The zombie half follows:
# a newer write on the same point settles strictly after the
# re-promoted boundary's own settle, the pair then switches to the
# peer, and the successor applies nothing — the stale suspended write
# never lands behind the newer command.
#
# Two consecutive passes must produce identical digests; the
# self-check leg plants a doctored settled-without-apply record, a
# doubled settle, a parked-accepted log, and a stale ordering through
# the audit — a silent audit reports gossip-repromote-settle-unchecked.
# Inconclusive when the staged run predates the contract — no
# receipt-level actor/reason attribution, no checkpoint receipt
# window or admission counters, or the demote never suspended the
# admission — or cannot reach the rig it names.

GOSSIP_SETTLE = 45     # bound on each demote, reconvergence, promote,
                       # switch, and role restore inside the leg
GOSSIP_AUDIT = 30      # bound on the suspended receipt's settle
                       # journaling on both peers
GOSSIP_POLL = 0.05     # wait cadence inside the leg — the gossip
                       # window is one pull period wide, so the
                       # re-promote follows the promotable verdict on
                       # the next cadence, not the next scan
GOSSIP_WATCH = 0.01    # cadence polling the demoted holder for the
                       # promotable verdict the re-promote rides — the
                       # gossip window is one pull period wide, so the
                       # promote follows the verdict on the next
                       # cadence
GOSSIP_ATTEMPTS = 4    # suspension-window submission bound — a
                       # submission the holder's own scan applied
                       # before the demote landed restages on the
                       # restored roles


def _gossip_promotable(report):
    """Whether a /role report shows a promotable standby — the
    converged verdict the re-promote needs, whatever the sync
    variant names the line it stands on."""
    return (report or {}).get('role') == 'standby' \
        and bool({'tracking', 'orphaned', 'reinitialized', 'usurped'}
                 & set((report or {}).get('sync') or {}))


def _gossip_served(ctx, base, admission):
    """The monitor's served receipt for the admission — matched on
    the (command, actor) submission identity — or None."""
    try:
        receipts, _ = _receipt_window(ctx, base)
    except Exception:
        return None
    return next((entry for entry in receipts
                 if _admission_hit(entry, admission)), None)


def _gossip_restore_roles(ctx, owner, peer, deadline):
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
            if _gossip_promotable(_try_role(ctx, ctx[owner])):
                _settle_call(ctx[owner] + '/promote')
        elif current is None:
            for name in (owner, peer):
                if _gossip_promotable(_try_role(ctx, ctx[name])):
                    _settle_call(ctx[name] + '/promote')
                    break
        time.sleep(GOSSIP_POLL)
    return _pair_active(ctx) == owner \
        and _tracking_standby(ctx, peer) is not None


def _gossip_snapshot(ctx, admission, floors, names):
    """One polled audit snapshot for an admission: each peer's
    journaled command_settled receipts, served receipt-log matches,
    and the image value at the admission's point, since the pass's
    journal floors. None while a peer drops a read — a lost
    observation, never the audit's verdict."""
    window = {'journaled': {}, 'logged': {}, 'image': {}}
    for name in names:
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


def _gossip_resolved(window, admission):
    """Whether the audit snapshot is terminal for the admission: both
    peers journaled the applied settle and both served logs agree — or
    a contradiction already showing, which no further wait heals."""
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
        return True           # a served log already shows an
                              # out-of-contract verdict
    if outcomes != {'applied'}:
        return False
    return all(counts.get(name) == 1
               and len(window['logged'][name]) == 1
               and _outcome_key(window['logged'][name][0]) == 'applied'
               for name in counts)


def _gossip_judge(window, durable, admission, note):
    """The admission's audit clauses over one resolved window plus the
    durable journal counts — records a violation for every clause the
    evidence breaks and answers 'single' or 'diverged' for the
    digest. `window` is _gossip_snapshot's journaled/logged/image
    record; `durable` maps each peer key to its --journal-file's
    matching command_settled receipts (or None when the file never
    read)."""
    label = 'the suspended admission ' + str(admission['actor']) \
        + ' (index ' + str(admission['index']) + ', point ' \
        + str(admission['point']) + ')'
    verdict = 'single'
    journaled, logged = window['journaled'], window['logged']
    names = tuple(journaled)
    # The parked-accepted check first — the defect this leg exists to
    # name: the receipt still Accepted on a live peer past the
    # re-promoted boundary.
    for name in names:
        if any(_outcome_key(receipt) == 'accepted'
               for receipt in logged[name]):
            verdict = 'diverged'
            note('parked-' + name, 'gossip-repromote-settle-failed',
                 label + ' is still parked Accepted on ' + name
                 + ' — the re-promoted holder\'s boundary never '
                 're-queued it')
    outcomes = {_outcome_key(receipt)
                for entries in journaled.values()
                for receipt in entries}
    if not outcomes:
        verdict = 'diverged'
        note('settle', 'gossip-repromote-settle-failed',
             label + ' journaled no command_settled on either peer '
             '— the re-promoted boundary never settled it')
    elif len(outcomes) != 1:
        verdict = 'diverged'
        note('outcomes', 'gossip-repromote-settle-nondeterministic',
             label + ' journaled ' + json.dumps(sorted(outcomes))
             + ' — one admission, never more than one terminal '
             'outcome')
    elif next(iter(outcomes)) != 'applied':
        verdict = 'diverged'
        note('outcome', 'gossip-repromote-settle-nondeterministic',
             label + ' settled ' + next(iter(outcomes))
             + ' — the re-promoted suspended receipt owes applied')
    # The journal coverage: exactly one command_settled per peer — the
    # re-promoted holder's own boundary plus the adopted record the
    # demoted sibling journals reconverging.
    for name in names:
        count = len(journaled[name])
        if count == 1:
            continue
        verdict = 'diverged'
        diagnostic = 'gossip-repromote-settle-failed' \
            if count == 0 else 'gossip-repromote-settle-nondeterministic'
        note('journal-' + name, diagnostic,
             label + ' journaled ' + str(count)
             + ' command_settled records on ' + name
             + ' — the contract owes exactly one there: the '
             + ('re-promoted holder\'s own boundary'
                if name == admission['demoted']
                else 'adopted record on the demoted sibling'))
    # The served logs: each peer's log carries the admission's receipt
    # settled applied — the terminal verdict, never a parked accepted
    # and never a diverging record.
    for name in names:
        receipts = logged[name]
        if len(receipts) > 1:
            verdict = 'diverged'
            note('log-count-' + name,
                 'gossip-repromote-settle-nondeterministic',
                 name + "'s served log carries " + str(len(receipts))
                 + ' receipts for ' + label)
        for receipt in receipts:
            served = _outcome_key(receipt)
            if served != 'applied' and served != 'accepted':
                verdict = 'diverged'
                note('log-' + name,
                     'gossip-repromote-settle-nondeterministic',
                     name + "'s served log carries " + served
                     + ' for ' + label
                     + ' where the journal settled applied')
        if 'applied' in outcomes and not receipts:
            verdict = 'diverged'
            note('log-missing-' + name,
                 'gossip-repromote-settle-nondeterministic',
                 name + "'s served log lost " + label
                 + ' the journal settled applied')
    # The durable half: each peer's --journal-file carries the same
    # one settle the serving monitor shows.
    for name in names:
        settles = durable.get(name)
        count = None if settles is None else len(settles)
        if count != 1:
            verdict = 'diverged'
            diagnostic = 'gossip-repromote-settle-failed' \
                if count == 0 \
                else 'gossip-repromote-settle-nondeterministic'
            note('durable-' + name, diagnostic,
                 name + "'s durable journal carries " + str(count)
                 + ' command_settled records for ' + label
                 + ' — the durable record owes exactly one there')
        elif _outcome_key(settles[0]) != 'applied':
            verdict = 'diverged'
            note('durable-outcome-' + name,
                 'gossip-repromote-settle-nondeterministic',
                 name + "'s durable journal settles " + label + ' '
                 + _outcome_key(settles[0])
                 + " — the line's one settlement is applied")
    # The image: the applied write present on both peers — a settle
    # whose command never applied is the doctored shape the leg names.
    for name in names:
        value = window['image'][name]
        if value != admission['value']:
            verdict = 'diverged'
            note('image-' + name, 'gossip-repromote-settle-failed',
                 label + ' settled but ' + name + "'s image serves "
                 + json.dumps(value) + ' — a settlement without the '
                 'command\'s application')
    return verdict


def _gossip_file_settles(path, admission, floor):
    """The durable journal file's `command_settled` receipts matching
    the admission past the pass's journal floor, or None while the
    file cannot be read — the durable half of the one-settle audit. The
    floor is the peer's own `seq` cursor: a durable journal file
    outlives one scenario run, so an earlier run's identically named
    admission is not this pass's record."""
    try:
        records = _journal_entries(path)
    except Exception:
        return None
    return [receipt for receipt in
            (_journal_settled(item)
             for item in records
             if isinstance(_gossip_entry_seq(item), int)
             and _gossip_entry_seq(item) > floor)
            if _admission_hit(receipt, admission)]


def _gossip_entry_seq(item):
    """A journal record's own `seq` — the durable file's `{'entry':
    {...}}` wrapper and a served journal entry alike."""
    body = item.get('entry') if isinstance(item.get('entry'), dict) \
        else item
    return (body or {}).get('seq')


def _gossip_applied_tick(receipt):
    """A settled `applied` receipt's own run tick, or None — the
    same-domain stamp the newer-command ordering reads."""
    applied = (receipt or {}).get('outcome') or {}
    tick = applied.get('applied')
    return tick.get('tick') if isinstance(tick, dict) else None


def _gossip_durable(ctx, journal_files, names, admission, floors):
    """Each peer's durable --journal-file settle count for one
    admission past its journal floor — None where the file cannot be
    read."""
    durable = {}
    for name in names:
        path = journal_files.get(name)
        if path is None:
            durable[name] = None
            continue
        durable[name] = _gossip_file_settles(path, admission,
                                             floors.get(name, 0))
    return durable


def _gossip_successor_judge(window, admission, note):
    """The successor audit's clauses: the switch to the peer that used
    to resurrect the stale write settled nothing a second time — one
    `command_settled` per admission per peer, all `applied`, never a
    parked `accepted`. The image is not read here: the newer write
    legitimately moved the point past this admission's value, so the
    served-value clause is the caller\'s, comparing against the newer
    command. Answers 'single' or 'diverged' for the digest."""
    label = 'the suspended admission ' + str(admission['actor'])
    verdict = 'single'
    for name, entries in window['journaled'].items():
        count = len(entries)
        if count == 1:
            if _outcome_key(entries[0]) != 'applied':
                verdict = 'diverged'
                note('successor-outcome-' + name,
                     'gossip-repromote-settle-nondeterministic',
                     name + "'s record for " + label + ' reads '
                     + _outcome_key(entries[0])
                     + ' after the switch — the one settlement owed is '
                     'applied')
            continue
        verdict = 'diverged'
        note('successor-count-' + name,
             'gossip-repromote-settle-failed'
             if count == 0
             else 'gossip-repromote-settle-nondeterministic',
             name + ' journaled ' + str(count) + ' command_settled '
             'records for ' + label + ' after the switch — a receipt '
             'that resolved at the re-promoted boundary is never '
             'applied again')
    for name, entries in window['logged'].items():
        if any(_outcome_key(receipt) == 'accepted'
               for receipt in entries):
            verdict = 'diverged'
            note('successor-parked-' + name,
                 'gossip-repromote-settle-failed',
                 name + "'s served log still parks " + label
                 + ' Accepted after the switch')
    return verdict


def _gossip_switch(ctx, owner, peer, deadline):
    """The documented pair switch: demote the field owner, promote
    the converged peer, and wait for the promoted peer to report
    `active` and the demoted one to reconverge tracking. Answers the
    promoted peer or None when the switch never landed."""
    _settle_call(ctx[owner] + '/demote')
    while time.monotonic() < deadline:
        status, _body = _settle_call(ctx[peer] + '/promote')
        if status == 200:
            break
        time.sleep(GOSSIP_POLL)
    promoted = wait_for(
        lambda: 'active' if (_try_role(ctx, ctx[peer]) or {}).get(
            'role') == 'active' else None,
        deadline, interval=GOSSIP_POLL)
    if promoted is None:
        return None
    wait_for(lambda: _tracking_standby(ctx, owner),
             deadline, interval=GOSSIP_POLL)
    return peer


def _gossip_stage(ctx, run_id, number, owner, peer, point, attempts,
                  round_index):
    """One staging round: the receipted writable-point write and the
    holder's own demote land back to back, so the demote boundary
    suspends the still-pending receipt `Accepted`. Returns the staged
    admission, `None` when the round proved nothing — an admission the
    holder's own scan applied before the demote closed the gate, a
    refused admission or demote — having recorded the attempt and
    restored the launch roles, `'vanished'` when the round's receipt
    disappeared from the holder's log outright, or a mapping carrying
    `attribution` when the served receipt dropped the declared
    actor/reason the settle audit correlates by."""
    owner_base = ctx[owner]
    current = _point_value(_snapshot(ctx, owner_base), point)
    attempt_value = not (current if isinstance(current, bool) else False)
    command = {'write_value': {'point': point, 'kind': 'bool',
                               'value': {'bool': attempt_value}}}
    actor = 'qa-gossip-' + run_id + '-' + str(number) + '-' \
        + str(round_index)
    admission = {'actor': actor, 'command': command, 'point': point,
                 'value': attempt_value, 'demoted': owner,
                 'promoted': peer,
                 'index': _next_receipt_index(ctx, owner_base)}
    attempt = {'actor': actor, 'index': admission['index'],
               'value': attempt_value}
    status, receipt = None, None
    try:
        status, receipt = http_json(
            'POST', owner_base + '/command',
            {'command': command, 'actor': actor,
             'reason': 'gossip-repromote-settle'})
    except urllib.error.HTTPError as exc:
        exc.close()
        status, receipt = exc.code, None
    except Exception as exc:
        status, receipt = None, str(exc)
    attempt['submission'] = {'status': status, 'receipt': receipt}
    if status != 200 or not isinstance(receipt, dict) \
            or _outcome_key(receipt) != 'accepted':
        attempt['missed'] = 'not admitted'
        attempts.append(attempt)
        _gossip_restore_roles(ctx, owner, peer,
                              time.monotonic() + GOSSIP_SETTLE)
        return None
    if receipt.get('actor') != actor \
            or receipt.get('reason') != 'gossip-repromote-settle':
        attempt['attribution'] = 'dropped by the served receipt'
        attempts.append(attempt)
        return {
            'attribution': 'the served receipt drops the declared '
            'actor/reason \u2014 the submission-record identity the '
            'settle audit correlates by \u2014 the rig predates the '
            'gossip-repromote-settle contract'}
    # The demote, immediately: the still-pending receipt suspends at
    # the demote's boundary.
    demote_status, demote_body = _settle_call(owner_base + '/demote')
    attempt['demote'] = {'status': demote_status, 'body': demote_body}
    attempts.append(attempt)
    if demote_status != 200:
        attempt['missed'] = 'demote refused'
        _gossip_restore_roles(ctx, owner, peer,
                              time.monotonic() + GOSSIP_SETTLE)
        return None
    served = _gossip_served(ctx, owner_base, admission)
    if served is None:
        attempt['missed'] = 'receipt vanished'
        return 'vanished'
    if _outcome_key(served) != 'accepted':
        # The holder's own scan applied the admission before the demote
        # closed the gate: nothing froze suspended, so this round
        # proves nothing. Restore and let the caller restage.
        attempt['missed'] = 'applied before the demote'
        _gossip_restore_roles(ctx, owner, peer,
                              time.monotonic() + GOSSIP_SETTLE)
        return None
    return admission


def _gossip_pass(ctx, number, owner, peer, point, value):
    """One gossip-window pass: stage a receipted write whose holder
    demotes back to back so the demote boundary suspends it, re-promote
    that same holder the moment it reports a promotable verdict —
    inside the gossip window, before the peer's next tracking pull can
    carry the admission back covered — audit both serving monitors and
    both durable journals that the suspended receipt settled exactly
    once at the re-taken boundary, then prove the zombie half: a newer
    write on the same point settles strictly after it, and the
    successor the pair switches to applies nothing. Returns (digest,
    violations, evidence)."""
    violations = {}
    evidence = {'pass': number, 'entry_owner': owner, 'point': point}
    digest = {'suspended': 'unstaged', 'reconverged': 'no',
              'window': 'unseen', 'repromoted': 'no', 'settle': 'unseen',
              'image': 'unseen', 'journals': 'unread', 'newer': 'unseen',
              'order': 'unseen', 'successor': 'unseen',
              'roles': 'unrestored'}
    owner_base, peer_base = ctx[owner], ctx[peer]
    journal_files = ctx.get('journal_files') or {}
    names = (owner, peer)

    def note(key, diagnostic, detail):
        violations.setdefault(key, (diagnostic, detail))

    def failed(key, detail):
        note(key, 'gossip-repromote-settle-failed', detail)

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

    def record_attempts():
        evidence['attempts'] = [dict(attempt) for attempt in attempts]

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
        for name in names:
            floors[name] = _journal_cursor(ctx, ctx[name])
    except Exception as exc:
        failed('floors', 'a peer\'s journal floor never served: '
               + str(exc)[:200])
        return finish(None)
    evidence['floors'] = floors
    # The run's own identity in every actor it declares: a durable
    # journal file and a bounded receipt log both outlive one scenario
    # run, so an identity that repeated across runs would let an
    # earlier run's record answer this pass's audit.
    run_id = str(max(floors.values()))

    # ---- stage the suspended admission -----------------------------
    #
    # The admission and its holder's demote land back to back: a scan
    # that beat the demote applied the command already, and a demote
    # the holder refused means the pair moved under the staging. Either
    # way no suspended admission froze, so the round restages.
    staged = None
    attempts = []
    for round_index in range(GOSSIP_ATTEMPTS):
        outcome = _gossip_stage(ctx, run_id, number, owner, peer, point,
                                attempts, round_index)
        record_attempts()
        if isinstance(outcome, dict) and 'attribution' in outcome:
            return inconclusive('attribution', outcome['attribution'])
        if outcome == 'vanished':
            failed('suspended-receipt', 'the staged admission\'s receipt '
                   'vanished from the holder\'s log outright')
            return finish(digest)
        if outcome is None:
            if not _gossip_restore_roles(ctx, owner, peer,
                                         time.monotonic() + GOSSIP_SETTLE):
                return inconclusive(
                    'restage', 'the pair never re-took the launch roles '
                    'after a missed staging round')
            continue
        staged = outcome
        break
    record_attempts()
    if staged is None:
        return inconclusive(
            'suspension', 'no admission froze suspended across '
            + str(GOSSIP_ATTEMPTS)
            + ' staged rounds — every demote landed past the holder\'s '
            'applying scan')

    # The suspended standing: the demoted holder\'s served receipt still
    # reads Accepted and its image never took the write — the shape a
    # covering adoption must never have reached.
    evidence['suspended_standing'] = _gossip_served(ctx, owner_base, staged)
    evidence['suspended_image'] = _point_value(
        _snapshot(ctx, owner_base), point)
    if evidence['suspended_image'] == staged['value']:
        failed('suspended-image', 'the demoted holder\'s image already '
               'serves the admitted value for point ' + str(point)
               + ' — the suspension wrote through the demote')
        return finish(digest)
    digest['suspended'] = 'accepted'

    # ---- the gossip window ------------------------------------------
    #
    # The demoted holder polls the sibling's checkpoint stream to a
    # promotable verdict on a tight cadence, and the promote follows on
    # the very next poll — inside the window in which the sibling's
    # next tracking pull would carry the admission back covered. The
    # leg's own traffic stays out of that window: the window is read
    # after the promote request, never before it.
    #
    # Which window the rig admitted is evidence, never a verdict: both
    # resolutions owe the same exactly-once contract, and the audit
    # below — never parked `Accepted`, settled exactly once per peer,
    # the newer command never overtaken, the successor applying
    # nothing — is what the defect broke. A rig whose peer's pull is
    # phase-locked ahead of the demoted holder's own scan closes the
    # window every time (the peer carries the admission covered and
    # the re-promoted boundary still settles it once); the open variant
    # is held open by construction on the consumer leg's driven pair,
    # which scans only the demoted holder between the demote and the
    # re-promote. Either way the pass records which window it staged.
    reconverged = None
    deadline = time.monotonic() + GOSSIP_SETTLE
    while time.monotonic() < deadline:
        report = _try_role(ctx, owner_base)
        if _gossip_promotable(report):
            reconverged = report
            break
        time.sleep(GOSSIP_WATCH)
    evidence['reconverged'] = reconverged
    if reconverged is None:
        failed('reconverge', 'the demoted holder never reached a '
               'promotable verdict on the sibling\'s checkpoint stream')
        return finish(digest)
    digest['reconverged'] = 'promotable'

    status, body = _settle_call(owner_base + '/promote')
    evidence['repromote'] = {'status': status, 'body': body}
    if status != 200:
        failed('repromote', 'POST /promote on the demoted holder '
               'answered ' + str(status) + ' ' + json.dumps(body)[:200])
        return finish(digest)
    digest['repromoted'] = 'yes'
    peer_index = _next_receipt_index(ctx, peer_base)
    peer_served = _gossip_served(ctx, peer_base, staged)
    evidence['pre_repromote'] = {
        'peer_next_index': peer_index, 'peer_receipt': peer_served,
        'window': 'open' if (peer_served is None
                             and peer_index <= staged['index'])
        else 'closed'}
    digest['window'] = 'observed'

    newer = {'write_value': {'point': point, 'kind': 'bool',
                             'value': {'bool': not staged['value']}}}

    # ---- the audit -------------------------------------------------
    #
    # Wait out the whole expected shape — the re-promoted holder's own
    # settle plus the demoted sibling's adopted record — then judge
    # whatever the last complete read saw.
    window = None
    deadline = time.monotonic() + GOSSIP_AUDIT
    while time.monotonic() < deadline:
        read = _gossip_snapshot(ctx, staged, floors, names)
        if read is not None:
            window = read
            if _gossip_resolved(read, staged):
                break
        time.sleep(GOSSIP_POLL)
    evidence['window'] = window
    if window is None:
        failed('window', 'the audit\'s window never read — a serving '
               'monitor dropped its journal, receipts, or snapshot '
               'inside the bound')
        return finish(digest)

    durable = _gossip_durable(ctx, journal_files, names, staged, floors)
    evidence['durable'] = durable

    digest['settle'] = _gossip_judge(
        window, durable, staged,
        lambda key, diagnostic, detail: note(
            key + '-' + str(staged['actor']), diagnostic, detail))
    digest['image'] = 'served' if all(
        window['image'][name] == staged['value']
        for name in names) else 'unserved'
    digest['journals'] = 'once-each' if not any(
        key.startswith('durable-') or key.startswith('journal-')
        for key in violations) else 'split'

    # ---- the zombie half -------------------------------------------
    #
    # A newer write on the same point, submitted on the re-promoted
    # holder: it must settle at a run tick strictly after the
    # re-promoted boundary's own settle of the suspended admission —
    # the older write can never land after it — and the switch that
    # used to resurrect the stale write must apply nothing.
    newer_admission = {'actor': 'qa-gossip-newer-' + run_id + '-'
                       + str(number),
                       'command': newer, 'point': point,
                       'value': not staged['value'], 'demoted': peer,
                       'promoted': owner}
    try:
        status, newer_receipt = http_json(
            'POST', owner_base + '/command',
            {'command': newer, 'actor': newer_admission['actor'],
             'reason': 'gossip-repromote-settle-newer'})
    except Exception as exc:
        status, newer_receipt = None, str(exc)
    evidence['newer_submission'] = {'status': status,
                                    'receipt': newer_receipt}
    if status != 200 or not isinstance(newer_receipt, dict) \
            or _outcome_key(newer_receipt) != 'accepted':
        failed('newer', 'the newer write on the same point answered '
               + str(status) + ' ' + json.dumps(newer_receipt)[:200]
               + ' — no fresh admission to order against')
        return finish(digest)
    newer_admission['index'] = _next_receipt_index(ctx, owner_base) - 1

    newer_window = None
    deadline = time.monotonic() + GOSSIP_AUDIT
    while time.monotonic() < deadline:
        read = _gossip_snapshot(ctx, newer_admission, floors, names)
        if read is not None:
            newer_window = read
            if _gossip_resolved(read, newer_admission):
                break
        time.sleep(GOSSIP_POLL)
    evidence['newer_window'] = newer_window
    if newer_window is None:
        failed('newer-window', 'the newer admission\'s audit window '
               'never read inside the bound')
        return finish(digest)
    digest['newer'] = _gossip_judge(
        newer_window, _gossip_durable(ctx, journal_files, names,
                                     newer_admission, floors),
        newer_admission,
        lambda key, diagnostic, detail: note(
            key + '-' + str(newer_admission['actor']), diagnostic,
            detail))
    served_newer = _gossip_served(ctx, owner_base, newer_admission)
    served_stale = _gossip_served(ctx, owner_base, staged)
    evidence['newer_served'] = {'stale': served_stale,
                                'newer': served_newer}
    stale_tick = _gossip_applied_tick(served_stale)
    newer_tick = _gossip_applied_tick(served_newer)
    if stale_tick is None or newer_tick is None \
            or newer_tick <= stale_tick:
        digest['order'] = 'inverted'
        note('order', 'gossip-repromote-settle-nondeterministic',
             'the newer write settled at ' + str(newer_tick)
             + ' against the re-promoted boundary\'s settle at '
             + str(stale_tick) + ' — the older suspended write must '
             'never apply after the newer command')
    else:
        digest['order'] = 'ordered'

    # The switch that used to resurrect the stale write: the successor
    # must serve the newer value and settle neither admission again.
    promoted = _gossip_switch(ctx, owner, peer,
                              time.monotonic() + GOSSIP_SETTLE)
    evidence['switched'] = promoted
    if promoted is None:
        failed('switch', 'the pair never landed the successor switch '
               '— the promoted peer never reported active')
        return finish(digest)
    stale_window = None
    deadline = time.monotonic() + GOSSIP_AUDIT
    while time.monotonic() < deadline:
        read = _gossip_snapshot(ctx, staged, floors, names)
        if read is not None:
            stale_window = read
            if _gossip_successor_judge(read, staged, lambda *a: None) \
                    == 'single':
                break
        time.sleep(GOSSIP_POLL)
    evidence['successor_window'] = stale_window
    if stale_window is None:
        failed('successor-window', 'the successor\'s audit window '
               'never read inside the bound')
        return finish(digest)
    # The successor owes the admission exactly the one settlement the
    # re-promoted boundary made — no second application, and the newer
    # value still standing on the point.
    digest['successor'] = _gossip_successor_judge(
        stale_window, staged,
        lambda key, diagnostic, detail: note(
            'successor-' + key + '-' + str(staged['actor']), diagnostic,
            detail))
    promoted_value = stale_window['image'][peer]
    if promoted_value != newer_admission['value']:
        digest['successor'] = 'diverged'
        note('successor-image', 'gossip-repromote-settle-failed',
             'the promoted successor serves ' + json.dumps(promoted_value)
             + ' for point ' + str(point) + ' after the switch — the '
             'newer value ' + json.dumps(newer_admission['value'])
             + ' the line settled stands, never the stale suspended '
             'write')

    # The launch roles: back on the entry owner, with the sibling
    # tracking behind it — the layout the cases behind this one enter
    # on.
    restored = _pair_active(ctx) == owner \
        and _tracking_standby(ctx, peer) is not None
    if not restored:
        restored = _gossip_restore_roles(ctx, owner, peer,
                                         time.monotonic()
                                         + GOSSIP_SETTLE)
    if restored:
        digest['roles'] = 'restored'
    else:
        failed('roles', 'the pair did not land back on the launch '
               'roles — owner ' + str(_pair_active(ctx)))
    return finish(digest)


def _gossip_self_check():
    """The leg's unchecked-diagnostic self-test: run the audit judge
    over each planted negative it must name — a settle the image never
    applied, a doubled journal, a parked-accepted log, and a missing
    durable record — and require the judge to note its named
    violation. A silent judge returns the negative names it let
    through."""
    admission = {'actor': 'self-check', 'point': 7, 'value': True,
                 'index': 3, 'demoted': 'active', 'promoted': 'standby',
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
        _gossip_judge(
            window, files, admission,
            lambda key, diagnostic, detail: found.append(diagnostic))
        return found

    # The planted settle-without-apply: the journal claims the verdict
    # but the image never took the write.
    window = clean()
    window['image']['active'] = False
    found = judge(window, durable)
    if 'gossip-repromote-settle-failed' not in found:
        slipped.append('settle-without-apply')
    # The planted double settle: two command_settled records for one
    # admission on its holder.
    window = clean()
    window['journaled']['active'] = [dict(settled), dict(settled)]
    found = judge(window, durable)
    if 'gossip-repromote-settle-nondeterministic' not in found:
        slipped.append('double-settle')
    # The planted parked receipt: the log still reads Accepted and no
    # journal ever settled it.
    window = clean()
    window['journaled'] = {'active': [], 'standby': []}
    window['logged']['active'] = [dict(parked)]
    found = judge(window, {'active': [], 'standby': []})
    if 'gossip-repromote-settle-failed' not in found:
        slipped.append('parked-accepted')
    # The planted silent durable half: the served monitors agree while
    # the durable record carries nothing.
    window = clean()
    found = judge(window, {'active': [], 'standby': [dict(settled)]})
    if 'gossip-repromote-settle-failed' not in found:
        slipped.append('missing-durable')

    def successor_judge(record):
        found = []
        _gossip_successor_judge(
            record, admission,
            lambda key, diagnostic, detail: found.append(diagnostic))
        return found

    # The planted successor re-application: the switch to the peer
    # settled the admission a second time.
    window = clean()
    window['journaled']['standby'] = [dict(settled), dict(settled)]
    found = successor_judge(window)
    if 'gossip-repromote-settle-nondeterministic' not in found:
        slipped.append('successor-reapply')
    # The planted successor parking: the promoted peer's served log
    # still reads the admission Accepted.
    window = clean()
    window['logged']['standby'] = [dict(parked)]
    found = successor_judge(window)
    if 'gossip-repromote-settle-failed' not in found:
        slipped.append('successor-parked')
    return slipped


def scenario_gossip_repromote_settle(ctx):
    """Exercise the gossip-window re-promote settle contract on the
    deployed pair: with the pair settled and tracking, submit a
    receipted writable-point command and demote the holder back to
    back so the demote's boundary suspends the receipt Accepted, poll
    the demoted holder to a promotable verdict and re-promote that
    same holder inside the gossip window — before the peer's next
    tracking pull can carry the admission back covered — then audit
    through both serving monitors and both durable journals that the
    suspended receipt settles exactly once at the re-taken boundary:
    the point serves the command's value, /receipts carries the
    terminal verdict rather than a parked accepted, one
    command_settled per admission stands on each peer's journal, a
    newer write on the same point settles strictly after it, and the
    successor the pair switches to applies nothing; two passes
    produce identical digests."""
    case = Case(
        'gossip-repromote-settle',
        'A holder re-promoted inside the gossip window settles its '
        'suspended receipt once',
        'with the deployed pair settled and tracking, submit a '
        'receipted writable-point command and demote the holder back '
        'to back so the demote boundary suspends the receipt '
        'Accepted, poll the demoted holder to a promotable verdict and '
        're-promote that same holder inside the gossip window — before '
        'the peer\'s next tracking pull can carry the admission back '
        'covered: through both serving monitors and both durable '
        'journals the suspended receipt settles exactly once at the '
        're-taken boundary — the point serves the command\'s value, '
        '/receipts carries the terminal verdict rather than a parked '
        'accepted, one command_settled per admission stands on each '
        'peer\'s journal, a newer write on the same point settles '
        'strictly after it and the successor the pair switches to '
        'applies nothing, and the pair\'s launch roles restore; two '
        'passes produce identical digests')
    owner, peer = 'active', 'standby'
    try:
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries only one endpoint — the pair '
                               'the gossip-window leg needs is absent')
        journal_files = ctx.get('journal_files') or {}
        missing = [name for name in (owner, peer)
                   if journal_files.get(name) is None]
        if missing:
            return case.finish('inconclusive', 'the run context '
                               'carries no journal files for '
                               + json.dumps(missing) + ' — the durable '
                               'half of the settle audit is absent')
        for name in (owner, peer):
            try:
                _role(ctx, ctx[name])
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s '
                                   'monitor is unreachable: '
                                   + str(exc)[:200])
        deadline = time.monotonic() + GOSSIP_SETTLE
        active = wait_for(lambda: _pair_active(ctx), deadline,
                          interval=GOSSIP_POLL)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        if active != owner:
            return case.finish('inconclusive', 'the field owner is '
                               + active + ' — the gossip-window '
                               'exercise needs the launched-active peer '
                               'owning the field; the pair\'s layout '
                               'predates the stage')
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=GOSSIP_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the settle the leg '
                               'suspends inside was never reached')
        # The contract surface: the owner's checkpoint must carry the
        # receipt window and admission counters the index correlation
        # reads, and the command path must echo the declared
        # actor/reason the admission identity rides.
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
                               'gossip-repromote-settle contract')
        _, signals = http_json('GET', ctx[owner] + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'gossip-settle-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming the writable '
                      'command point')
        points = _writable_bool_points(signals, 1)
        if not points:
            return case.finish('inconclusive', 'the model declares no '
                               'writable bool command point')
        point = points[0]
        snapshot = _snapshot(ctx, ctx[owner])
        baseline = _point_value(snapshot, point)
        if not isinstance(baseline, bool):
            baseline = False
        value = not baseline
        # The attribution probe: a receipted admission echoing its
        # declared actor and reason — the submission-record identity
        # the settle audit correlates by — before any induction.
        status, probe = http_json(
            'POST', ctx[owner] + '/command',
            {'command': {'write_value': {
                'point': point, 'kind': 'bool',
                'value': {'bool': baseline}}},
             'actor': 'qa-gossip-contract-probe',
             'reason': 'gossip-repromote-settle-contract'})
        if status != 200 or _outcome_key(probe) != 'accepted':
            return case.finish('inconclusive', 'the contract probe '
                               'submission drew no admission: '
                               + str(status) + ' '
                               + json.dumps(probe)[:200])
        if probe.get('actor') != 'qa-gossip-contract-probe' \
                or probe.get('reason') \
                != 'gossip-repromote-settle-contract':
            return case.finish('inconclusive', 'the served receipt '
                               'drops the declared actor/reason — the '
                               'rig predates the '
                               'gossip-repromote-settle contract')
        case.observe('field owner: ' + owner + ' (' + ctx[owner]
                     + '); sibling standby: ' + peer
                     + '; suspended point ' + str(point))
        digests = []
        windows = []
        try:
            for number in (1, 2):
                digest, violations, evidence = _gossip_pass(
                    ctx, number, owner, peer, point, value)
                windows.append((evidence.get('pre_repromote') or {})
                               .get('window'))
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'gossip-settle-pass-' + str(number) + '.json',
                    evidence)
                case.evidence('file', ref, 'gossip-window settle pass '
                              + str(number) + ' — the admission and '
                              'its holder demote back to back, the '
                              'demoted holder\'s promotable verdict, '
                              'the in-window re-promote, the monitor '
                              'and durable journal audits, the newer '
                              'write and successor switch, and the '
                              'normalized digest')
                if evidence.get('inconclusive'):
                    return case.finish('inconclusive',
                                       evidence['inconclusive'])
                if violations or digest is None:
                    diagnostic = 'gossip-repromote-settle-failed' \
                        if digest is None or any(
                            name == 'gossip-repromote-settle-failed'
                            for name, _ in violations.values()) \
                        else 'gossip-repromote-settle-nondeterministic'
                    return case.finish(
                        'failed', diagnostic + ': ' + '; '.join(
                            detail for _, detail in
                            list(violations.values())[:4]))
                digests.append(digest)
        finally:
            # The launch layout for the cases behind this one: a clean
            # pass restores it by construction; an aborted pass may
            # have left the peer owning the field — demote whichever
            # peer still owns it, promote the entry owner, and let the
            # pair reconverge.
            try:
                restored = _gossip_restore_roles(
                    ctx, owner, peer, time.monotonic() + GOSSIP_SETTLE)
                if not restored:
                    case.observe('cleanup: the pair did not settle back '
                                 'to the launch roles')
            except Exception as exc:
                case.observe('cleanup: role restore failed: '
                             + str(exc)[:200])
        if digests[0] != digests[1]:
            return case.finish(
                'failed',
                'gossip-repromote-settle-nondeterministic: the two '
                'passes\' digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two gossip-window settle passes, identical '
                     'digests')
        case.observe('the re-promote landed inside the gossip window in '
                     + str(windows.count('open')) + ' of '
                     + str(len(windows))
                     + ' passes (the peer carried the admission covered '
                     'in the rest)')

        # The unchecked self-check: each audit clause, run over a
        # planted negative, must name its violation — a silent audit
        # can no longer be trusted to catch what it names.
        slipped = _gossip_self_check()
        if slipped:
            return case.finish(
                'failed',
                'gossip-repromote-settle-unchecked: the settle audit '
                'stayed silent on planted negatives: '
                + ', '.join(slipped))
        case.observe('the self-check leg\'s planted negatives each '
                     'reported their named diagnostic')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))