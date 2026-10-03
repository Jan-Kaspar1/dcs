"""The suspended_alias_audit acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the suspended-alias leg shares the launch-layout window
# behind the demote/carry legs — it needs the settled tracking pair,
# the driven third-controller seam, the controller stop/start seam,
# and the claim wire, and it restores the launch roles before the
# tune case's a->b switch.
RUNS_BEFORE = frozenset({'scenario_parameter_tune_carryover'})


# --------------------------------------------------------------------
# The suspended-receipt alias-audit contract — the lane evidence for
# #1080's fix serving WW-FND-004's receipted-command guarantee and
# decision 95's suspended-entry duty. The rig's carry legs exercise
# suspended-command *carriage* — the verbatim receipt riding the
# adopted window — but never the identical-command aliasing path the
# finding drove: a receipted command suspended at a fenced demote
# must keep its audit identity when the adopted receipt window
# carries an identical *command* at its absolute index. The adopted
# entry is the suspended submission's carry only when it is the same
# submission — the verbatim receipt or its terminal settlement, the
# whole record including `actor` and `reason`; an identical
# (point, value) write minted by a different admission is a different
# command at the index, and the suspended entry resolves
# `Rejected(Superseded)` journaled once — never absorbed as the
# promoted peer's carry, never vanished unaudited.
#
# Staging the collision deterministically takes the run's driven
# third controller: only a peer whose checkpoint pulls are frozen can
# mint the identical command at the suspended admission's index —
# every paced follower's pulls carry the suspended receipt verbatim
# and its mint lands one index higher. The leg converges the driven
# peer behind the suspended admission's holder, then stops the
# holder itself: every source the promote boundary's final-sync
# fetch could pull — the resolved track, the configured --standby,
# the field's claimed monitor — serves the holder's checkpoint, so
# the stopped holder is the only stale fetch. The promoted peer's
# adopted window predates the suspended admission, its submission
# high-water mints the identical command at the collision index,
# and the restarted holder's covering adoption — re-resolving onto
# the promoted run through the field's claimed monitor — is the
# adjudication the contract owes.
#
# The audit reads both peers' serving monitors — the suspended
# holder's and the promoted peer's — and both durable journals:
# the suspended admission is adjudicated `rejected:superseded`
# journaled exactly once on its holder, or still suspended and
# attributable while the covering path is open, never silently
# dropped and never absorbed under the identical admission's
# identity; the promoted peer's identical command settles `applied`
# once on its own receipt index; and each admission carries exactly
# one command_settled per peer across the pair's journals when the
# covering path resolves. Two consecutive passes must produce
# identical digests; the self-check leg plants a doctored
# vanished-admission record through the audit — a silent audit
# reports suspended-alias-audit-unchecked. Inconclusive when the
# staged run predates the contract — no receipt-level actor/reason
# attribution, no checkpoint receipt window or admission counters,
# no driven-controller or lifecycle seam — or cannot reach the rig
# it names.

ALIAS_SETTLE = 45     # bound on each demote, promote, re-resolution,
                      # and role restore inside the leg
ALIAS_AUDIT = 30      # bound on the covering adjudication and the
                      # identical admission's settles journaling
ALIAS_POLL = 0.4      # wait cadence inside the leg
ALIAS_WATCH = 0.05    # cadence polling the superseded peer
                      # mid-demotion — the demoting walk is one scan
ALIAS_SCANS = 4       # driven scans per batch


def _tracking(report):
    """Whether a /role report shows a tracking standby — the
    reconverged posture the covering adoption rides."""
    return (report or {}).get('role') == 'standby' \
        and 'tracking' in ((report or {}).get('sync') or {})


def _drive(ctx, scans=ALIAS_SCANS):
    """One POST /scan batch on the driven peer — every pull it ever
    performs happens inside the request."""
    return http_json('POST', ctx['driven'] + '/scan',
                     {'scans': scans}, timeout=scans * 2 + 15)


def _index_receipt(ctx, base, index):
    """The served receipt at absolute submission `index`, or None
    while the retained window does not cover it — the promoted
    peer's own-index proof for the identical admission."""
    try:
        receipts, base_index = _receipt_window(ctx, base)
    except Exception:
        return None
    position = index - base_index
    if 0 <= position < len(receipts):
        return receipts[position]
    return None


def _alias_window(ctx, admissions, floors):
    """One polled audit snapshot for the pass's two admissions: each
    peer's journaled command_settled receipts and served receipt-log
    matches, keyed by peer then admission actor. None while any peer
    drops a read — a lost observation, never the audit's verdict."""
    window = {'journaled': {}, 'logged': {}}
    for name in ('active', 'standby', 'driven'):
        if ctx.get(name) is None or name not in floors:
            continue
        try:
            _, journal = http_json('GET', ctx[name] + '/journal?since='
                                   + str(floors[name]))
            _, receipts = http_json('GET', ctx[name] + '/receipts')
        except Exception:
            return None
        settled = [receipt for receipt in
                   (_journal_settled(entry)
                    for entry in _journal_list(journal))
                   if receipt is not None]
        logged = _receipt_list(receipts)
        window['journaled'][name] = {
            admission['actor']: [receipt for receipt in settled
                                 if _admission_hit(receipt, admission)]
            for admission in admissions}
        window['logged'][name] = {
            admission['actor']: [receipt for receipt in logged
                                 if _admission_hit(receipt, admission)]
            for admission in admissions}
    return window


def _suspended_verdict(journaled, logged, admission, note):
    """The suspended admission's audit verdict once the covering
    adoption landed: `journaled` is the holder's command_settled
    receipts matching the admission and `logged` its served-log
    matches. Adjudicated-by-name or still suspended and attributable
    are the contract's only shapes — a vanished admission is the
    unaudited drop the finding drove. Returns the digest verdict."""
    label = 'the suspended admission ' + str(admission['actor']) \
        + ' (point ' + str(admission['point']) + ', index ' \
        + str(admission['index']) + ')'
    if len(journaled) > 1:
        note('suspended-count',
             'suspended-alias-audit-nondeterministic',
             label + ' journaled ' + str(len(journaled))
             + ' command_settled records on its holder — one '
             'admission owes exactly one terminal boundary')
        return 'diverged'
    if journaled:
        outcome = _outcome_key(journaled[0])
        if outcome != 'rejected:superseded':
            note('suspended-outcome',
                 'suspended-alias-audit-nondeterministic',
                 label + ' settled ' + outcome + ' — the '
                 'adopted-window collision\'s only adjudication is '
                 'the named superseded rejection')
            return 'diverged'
        return 'adjudicated'
    if any(_outcome_key(receipt) == 'accepted' for receipt in logged):
        # Still suspended and attributable — the covering path has
        # not resolved; the receipt keeps its own audit identity.
        return 'suspended'
    note('suspended-vanished', 'suspended-alias-audit-failed',
         label + ' vanished unaudited — no journaled settle on '
         'its holder and no attributable receipt in its served '
         'log: the adopted window\'s identical entry aliased it '
         'as carried')
    return 'vanished'


def _identical_verdict(journaled, indexed, admission, note):
    """The promoted peer's identical-admission audit: `journaled`
    is every peer's served command_settled matches for the
    admission, keyed by peer, and `indexed` the promoted peer's
    served receipt at the collision index. Exactly one applied on
    each peer the covering path carried the line's record through —
    the admission settles on its own index, never under the
    suspended submission's identity."""
    label = 'the promoted peer\'s identical admission ' \
        + str(admission['actor']) + ' (point ' \
        + str(admission['point']) + ', index ' \
        + str(admission['index']) + ')'
    verdict = 'applied'
    for name, entries in journaled.items():
        if len(entries) != 1:
            diagnostic = 'suspended-alias-audit-failed' \
                if not entries else \
                'suspended-alias-audit-nondeterministic'
            note('identical-count-' + name, diagnostic,
                 label + ' journaled ' + str(len(entries))
                 + ' command_settled records on ' + name
                 + ' — the covering path owes exactly one '
                 'there')
            verdict = 'diverged'
            continue
        outcome = _outcome_key(entries[0])
        if outcome != 'applied':
            note('identical-outcome-' + name,
                 'suspended-alias-audit-nondeterministic',
                 label + ' settled ' + outcome + ' on ' + name
                 + ' — the identical admission\'s only verdict '
                 'is applied')
            verdict = 'diverged'
    if not _admission_hit(indexed, admission) \
            or _outcome_key(indexed) != 'applied':
        note('identical-index', 'suspended-alias-audit-failed',
             label + ' did not settle applied on the suspended '
             'admission\'s own receipt index — the promoted peer\'s '
             'served log carries ' + json.dumps(indexed)[:200]
             + ' there')
        verdict = 'diverged'
    return verdict


def _alias_pass(ctx, number, owner, peer, point, value):
    """One suspended-alias pass: freeze the holder so the promoted
    peer's final-sync pull misses the suspended admission, fence the
    field owner into a demote that suspends the receipted command,
    promote the driven peer's stale window, mint the identical
    command at the collision index, then audit the suspended
    admission's adjudication and the identical admission's own-index
    settle through both serving monitors and both durable journals.
    Returns (digest, violations, evidence): digest is the pass's
    normalized verdict record, identical across clean passes."""
    violations = {}
    evidence = {'entry_owner': owner, 'pass': number}
    digest = {'suspended': 'unseen', 'identical': 'unseen',
              'alias': 'unstaged', 'roles': 'unrestored'}
    base, peer_base, driven_base = ctx[owner], ctx[peer], ctx['driven']

    def note(key, diagnostic, detail):
        violations.setdefault(key, (diagnostic, detail))

    def failed(key, detail):
        note(key, 'suspended-alias-audit-failed', detail)

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

    # Converge the driven peer onto the holder — every batched scan
    # is a checkpoint pull on the source the leg freezes next.
    converged = None
    deadline = time.monotonic() + ALIAS_SETTLE
    while converged is None and time.monotonic() < deadline:
        try:
            _drive(ctx)
        except Exception as exc:
            failed('driven-scan', 'the driven peer\'s /scan batch '
                   'never answered: ' + str(exc)[:200])
            return finish(None)
        converged = _tracking_standby(ctx, 'driven')
        if converged is None:
            time.sleep(ALIAS_POLL)
    evidence['driven_converged'] = converged
    if converged is None:
        failed('driven-converge', 'the driven peer never reported a '
               'tracking standby on ' + owner)
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

    # The collision index: the absolute submission index the driven
    # peer's stale window mints next — the field owner's next index
    # must agree, else the converged line never stood and the
    # identical command cannot land on the suspended admission's
    # index.
    alias_index = _next_receipt_index(ctx, driven_base)
    owner_index = _next_receipt_index(ctx, base)
    evidence['indices'] = {'driven': alias_index, 'owner': owner_index}
    if alias_index != owner_index:
        failed('indices', 'the converged peers disagree on the '
               'submission high-water — the driven peer mints at '
               + str(alias_index) + ' where the owner mints at '
               + str(owner_index) + ' — the identical command '
               'cannot alias the suspended admission')
        return finish(digest)

    # The preemption the suspension window needs: a rogue writer
    # takes the field claim under the standing owner, so the
    # still-reporting-active gate admits the suspended submission
    # and the detection scan demotes the holder in place — the
    # fenced demote the issue names, its pending commands
    # suspended rather than settled.
    stream = _plant_connect(ctx)
    try:
        preempt = _plant_request(
            stream, {'op': 'claim_writer', 'owner': CLAIM_ROGUE})
    finally:
        stream.close()
    evidence['preempt'] = preempt
    if preempt.get('result') != 'done':
        failed('preempt', 'the rogue claim never preempted the '
               'owner — the fenced demote never ran: '
               + json.dumps(preempt)[:300])
        return finish(digest)

    # The suspended admission: identical point and value to the
    # promoted peer's coming command, a distinct admission identity.
    # A submission refused inside the closing window retries while
    # the owner still reports active.
    command = {'write_value': {'point': point, 'kind': 'bool',
                               'value': {'bool': value}}}
    suspended = {'command': command, 'point': point, 'value': value,
                 'index': alias_index, 'holder': owner,
                 'actor': 'qa-alias-susp-' + str(number),
                 'reason': 'suspended-alias-audit-' + str(number)}
    identical = {'command': command, 'point': point, 'value': value,
                 'index': alias_index, 'holder': 'driven',
                 'actor': 'qa-alias-ident-' + str(number),
                 'reason': 'suspended-alias-ident-' + str(number)}
    admissions = [suspended, identical]
    evidence['admissions'] = admissions
    receipt = None
    last = None
    deadline = time.monotonic() + ALIAS_SETTLE
    while receipt is None and time.monotonic() < deadline:
        try:
            status, body = http_json('POST', base + '/command',
                                     {'command': command,
                                      'actor': suspended['actor'],
                                      'reason': suspended['reason']})
        except Exception as exc:
            status, body = None, str(exc)
        last = (status, body)
        if status == 200 \
                and _outcome_key(body) == 'accepted':
            receipt = body
            break
        report = _try_role(ctx, base)
        if (report or {}).get('role') not in (None, 'active'):
            break           # the detection scan already demoted it
        time.sleep(ALIAS_WATCH)
    evidence['suspended_submission'] = {'status': last[0] if last
                                        else None,
                                        'receipt': receipt or
                                        (last[1] if last else None)}
    if receipt is None:
        failed('suspended-submit', 'the suspended-window '
               'submission never drew an admission — the fenced '
               'demote landed before it: '
               + json.dumps(last)[:300])
        return finish(digest)
    if receipt.get('actor') != suspended['actor'] \
            or receipt.get('reason') != suspended['reason']:
        return inconclusive(
            'attribution', 'the served receipt drops the declared '
            'actor/reason — the submission-record identity the '
            'contract compares — the rig predates the '
            'suspended-alias-audit contract')

    # The demote-in-place watch: the superseded peer's reported role
    # walks demoting to standby with its monitor answering every
    # poll, the receipt still Accepted at its index — the
    # suspension the successor's adopted window will collide with.
    watch = {'polls': 0, 'answered': 0, 'roles': []}
    settled = None
    deadline = time.monotonic() + ALIAS_SETTLE
    while time.monotonic() < deadline and settled is None:
        watch['polls'] += 1
        report = _try_role(ctx, base)
        if report is not None:
            watch['answered'] += 1
            watch['roles'].append(report.get('role'))
            if report.get('role') == 'standby':
                settled = report
        if settled is None:
            time.sleep(ALIAS_WATCH)
    evidence['demote_watch'] = watch
    if settled is None:
        failed('demote', 'the superseded peer never demoted — its '
               'reported role stayed '
               + json.dumps(watch['roles'][-3:]))
        return finish(digest)
    digest['alias'] = 'staged'
    standing = _index_receipt(ctx, base, alias_index)
    evidence['suspended_standing'] = standing
    if not _admission_hit(standing, suspended) \
            or _outcome_key(standing) != 'accepted':
        failed('suspended-receipt', 'the suspended admission\'s '
               'served receipt never read Accepted at index '
               + str(alias_index) + ' — the suspension window '
               'never staged: ' + json.dumps(standing)[:200])
        return finish(digest)
    digest['alias'] = 'engaged'

    # Freeze the adopted window: every live source the promoted
    # peer's final-sync fetch could pull — its resolved track, its
    # configured --standby, the field's claimed monitor — serves
    # the suspended admission's checkpoint, and a landed fetch
    # carries the receipt into the promotion. Stopping the holder
    # leaves the fetch nothing to pull: the promoted peer's window
    # predates the admission and its submission high-water mints
    # the identical command at the collision index.
    try:
        ctx['stop_controller'](owner)
    except Exception as exc:
        return inconclusive('holder-stop', 'the runner\'s '
                            'stop_controller action on ' + owner
                            + ' never completed: ' + str(exc)[:200])
    evidence['holder_stopped'] = True

    # The promoted peer: its final-sync fetch meets the stopped
    # holder — no live document carries the suspended admission —
    # so its submission high-water still mints at the collision
    # index.
    promote_status, promoted = _settle_call(driven_base + '/promote')
    evidence['driven_promote'] = {'status': promote_status,
                                  'body': promoted}
    if promote_status != 200:
        failed('driven-promote', 'POST /promote on the driven '
               'peer answered ' + str(promote_status) + ' '
               + json.dumps(promoted)[:200])
        return finish(digest)
    owned = None
    deadline = time.monotonic() + ALIAS_SETTLE
    while owned is None and time.monotonic() < deadline:
        try:
            _drive(ctx, 2)
        except Exception:
            pass
        report = _try_role(ctx, driven_base)
        if (report or {}).get('role') == 'active':
            owned = report
        else:
            time.sleep(ALIAS_POLL)
    evidence['driven_owned'] = owned
    if owned is None:
        failed('driven-active', 'the promoted peer never settled '
               'into the active role')
        return finish(digest)

    # The identical admission: same point, same value, minted at
    # the suspended admission's absolute index — the adopted
    # window's identical entry a command-only carry test would
    # alias.
    mint_index = _next_receipt_index(ctx, driven_base)
    evidence['mint_index'] = mint_index
    if mint_index != alias_index:
        failed('alias-index', 'the promoted peer\'s submission '
               'high-water moved to ' + str(mint_index)
               + ' — the identical command cannot mint at the '
               'suspended admission\'s index ' + str(alias_index)
               + ' — its final-sync pull carried the window')
        return finish(digest)
    try:
        status, body = http_json(
            'POST', driven_base + '/command',
            {'command': command, 'actor': identical['actor'],
             'reason': identical['reason']})
    except Exception as exc:
        status, body = None, str(exc)
    evidence['identical_submission'] = {'status': status,
                                        'receipt': body}
    if status != 200 or _outcome_key(body) != 'accepted':
        failed('identical-submit', 'the identical submission on '
               'the promoted peer answered ' + str(status) + ' '
               + json.dumps(body)[:200])
        return finish(digest)
    if body.get('actor') != identical['actor']:
        return inconclusive(
            'attribution', 'the promoted peer\'s served receipt '
            'drops the declared actor — the submission-record '
            'identity the contract compares — the rig predates '
            'the suspended-alias-audit contract')
    try:
        _drive(ctx, ALIAS_SCANS)
    except Exception as exc:
        failed('driven-scan', 'the identical admission\'s apply '
               'scans never answered: ' + str(exc)[:200])
        return finish(digest)

    # The covering path: the holder restarts — its suspended
    # receipt re-queues from the state file — and both demoted
    # peers re-resolve onto the promoted owner, the field's
    # claimed monitor and the propagated line owner naming it.
    try:
        ctx['start_controller'](owner)
    except Exception as exc:
        return inconclusive('holder-start', 'the runner\'s '
                            'start_controller action on ' + owner
                            + ' never completed: ' + str(exc)[:200])
    evidence['holder_restarted'] = True
    resolved = None
    reports = {}
    deadline = time.monotonic() + ALIAS_SETTLE
    while resolved is None and time.monotonic() < deadline:
        try:
            _drive(ctx, 2)
        except Exception:
            pass
        reports = {name: _try_role(ctx, ctx[name])
                   for name in (owner, peer, 'driven')}
        if (reports['driven'] or {}).get('role') == 'active' \
                and _tracking(reports[owner]) \
                and _tracking(reports[peer]):
            resolved = reports
        else:
            time.sleep(ALIAS_POLL)
    evidence['resolved'] = resolved
    if resolved is None:
        failed('resolution', 'the demoted pair never reconverged '
               'onto the promoted owner — the covering adoption '
               'the suspended admission waits on never landed: '
               + json.dumps(reports)[:300])
        return finish(digest)

    # The audit: the holder's journaled command_settled for the
    # suspended admission lands with the covering adoption —
    # adjudicated by name — while the identical admission settles
    # applied once on each peer that held the line's record. The
    # wait covers the whole expected shape — the adopted settles
    # trail the suspended adjudication by a pull or a flush, and a
    # snapshot taken at the first signal would read the covering
    # path mid-flight.
    def covered(snapshot):
        if snapshot is None:
            return None
        journaled = snapshot['journaled']
        if not journaled.get(owner, {}).get(suspended['actor']):
            return None
        for name in (owner, peer, 'driven'):
            if not journaled.get(name, {}).get(identical['actor']):
                return None
        return snapshot

    deadline = time.monotonic() + ALIAS_AUDIT
    window = None
    while time.monotonic() < deadline:
        window = covered(_alias_window(ctx, admissions, floors)) \
            or window
        if window is not None:
            break
        time.sleep(ALIAS_POLL)
    if window is None:
        # The bound expired mid-resolution — audit whatever the last
        # complete read saw; an unreachable peer reads None.
        window = _alias_window(ctx, admissions, floors)
    evidence['window'] = window
    if window is None:
        failed('window', 'the audit\'s window never read — a '
               'serving monitor dropped its journal or receipts '
               'inside the bound')
        return finish(digest)

    journaled = window['journaled']
    logged = window['logged']
    digest['suspended'] = _suspended_verdict(
        journaled.get(owner, {}).get(suspended['actor'], []),
        logged.get(owner, {}).get(suspended['actor'], []),
        suspended, note)
    indexed = _index_receipt(ctx, driven_base, alias_index)
    evidence['identical_indexed'] = indexed
    digest['identical'] = _identical_verdict(
        {name: journaled.get(name, {}).get(identical['actor'], [])
         for name in (owner, peer, 'driven')},
        indexed, identical, note)
    # The suspended admission on the promoted peer's side: it never
    # held it, so no settle may name it — an aliased carry minting
    # under the suspended receipt's identity would leave the
    # identical admission's journal the only record.
    stray = journaled.get('driven', {}).get(suspended['actor'], [])
    if stray:
        note('suspended-stray',
             'suspended-alias-audit-nondeterministic',
             'the promoted peer journaled ' + str(len(stray))
             + ' command_settled records for the suspended '
             'admission it never held — the alias leaked across '
             'the peers')
    # The sibling's evidence: it never held the suspended admission
    # either — a settle naming it must be the same single
    # superseded adjudication, at most once.
    sibling_settles = journaled.get(peer, {}).get(
        suspended['actor'], [])
    if len(sibling_settles) > 1 \
            or (sibling_settles and _outcome_key(sibling_settles[0])
                != 'rejected:superseded'):
        note('suspended-sibling',
             'suspended-alias-audit-nondeterministic',
             'the sibling journaled ' + str(len(sibling_settles))
             + ' command_settled records for the suspended '
             'admission — ' + json.dumps(
                 [_outcome_key(r) for r in sibling_settles]))
    # The durable half: the run's journal files carry the same one
    # settle per admission per peer the serving monitors show.
    journal_files = ctx.get('journal_files') or {}
    durable = {}
    for name in (owner, peer, 'driven'):
        path = journal_files.get(name)
        if path is None:
            continue
        try:
            records = _journal_entries(path)
        except Exception as exc:
            failed('durable-' + name, name + '\'s journal file '
                   'never read: ' + str(exc)[:200])
            continue
        settles = [receipt for receipt in
                   (_journal_settled(item) for item in records)
                   if receipt is not None]
        durable[name] = {
            admission['actor']: [
                _outcome_key(receipt) for receipt in settles
                if _admission_hit(receipt, admission)]
            for admission in admissions}
    evidence['durable'] = durable
    for name in (owner, peer, 'driven'):
        if name not in durable:
            continue
        holder_settles = durable[name].get(suspended['actor'], [])
        if len(holder_settles) > 1:
            note('durable-suspended-' + name,
                 'suspended-alias-audit-nondeterministic',
                 name + '\'s durable journal carries '
                 + str(len(holder_settles))
                 + ' command_settled records for the suspended '
                 'admission — one admission owes at most one')
        elif holder_settles \
                and holder_settles[0] != 'rejected:superseded':
            note('durable-suspended-' + name,
                 'suspended-alias-audit-nondeterministic',
                 name + '\'s durable journal settles the suspended '
                 'admission ' + str(holder_settles[0])
                 + ' — the collision\'s only adjudication is the '
                 'named superseded rejection')
        elif name == owner and not holder_settles:
            note('durable-suspended-' + name,
                 'suspended-alias-audit-failed',
                 'the suspended admission\'s holder journaled no '
                 'durable command_settled for it — the adjudication '
                 'the monitor served never reached the durable '
                 'record')
        identical_settles = durable[name].get(
            identical['actor'], [])
        if len(identical_settles) != 1:
            diagnostic = 'suspended-alias-audit-failed' \
                if not identical_settles else \
                'suspended-alias-audit-nondeterministic'
            note('durable-identical-' + name, diagnostic,
                 name + '\'s durable journal carries '
                 + str(len(identical_settles))
                 + ' command_settled records for the identical '
                 'admission — the covering path owes exactly one '
                 'there')

    # Restore the launch roles: demote the driven owner onto its
    # configured --standby source (the restarted sibling),
    # re-promote the entry owner, and reconverge every peer.
    restore_status, restore = _settle_call(driven_base + '/demote')
    evidence['restore_demote'] = {'status': restore_status,
                                  'body': restore}
    if restore_status != 200:
        failed('restore', 'POST /demote on the driven owner '
               'answered ' + str(restore_status) + ' '
               + json.dumps(restore)[:200])
        return finish(digest)
    promoted_back, last = None, None
    deadline = time.monotonic() + ALIAS_SETTLE
    while promoted_back is None and time.monotonic() < deadline:
        status, body = _settle_call(base + '/promote')
        if status == 200:
            promoted_back = body
        else:
            last = (status, body)
            time.sleep(ALIAS_POLL)
    evidence['restore_promote'] = {'body': promoted_back,
                                   'last_refusal': last}
    if promoted_back is None:
        failed('restore', 'the entry owner\'s promote never '
               'succeeded: ' + json.dumps(last)[:300])
        return finish(digest)
    restored = None
    deadline = time.monotonic() + ALIAS_SETTLE
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
            time.sleep(ALIAS_POLL)
    evidence['restored'] = restored
    if restored is None:
        failed('restore', 'the rig never settled back to the '
               'entry role layout: ' + json.dumps(reports)[:300])
        return finish(digest)
    digest['roles'] = 'restored'
    return finish(digest)


def scenario_suspended_alias_audit(ctx):
    """Exercise the #1080 suspended-receipt alias-audit contract on
    the deployed pair: fence a receipted writable-point command
    into the holder's demote suspension window, promote the driven
    third controller's stale window so its identical command mints
    at the suspended admission's absolute index, and audit through
    both serving monitors and both durable journals that the
    suspended admission is adjudicated by name or stays suspended
    and attributable — never vanished, never absorbed as the
    identical admission's carry — while the identical command
    settles applied once on its own index and each admission
    carries exactly one command_settled per peer."""
    case = Case(
        'suspended-alias-audit',
        'Suspended receipt survives an identical adopted-window command',
        'with the deployed pair settled and tracking, a receipted '
        'writable-point command suspended at a fenced demote — the '
        'driven third controller converged behind the holder, the '
        'holder stopped so the promote boundary\'s final-sync '
        'fetch misses the suspended admission — while the promoted '
        'peer\'s identical command (same point, same value, a '
        'distinct admission identity) mints at the suspended '
        'admission\'s absolute index; through both peers\' serving '
        'monitors and durable journals the suspended admission '
        'keeps its own receipt identity — adjudicated '
        'rejected:superseded once on its holder, or still suspended '
        'and attributable, never silently dropped or absorbed as '
        'the promoted peer\'s carry — the identical command settles '
        'applied once on its own index, each admission carries '
        'exactly one command_settled per peer, the launch roles '
        'restore, and two passes produce identical digests')
    owner = peer = None
    try:
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries only one endpoint — the '
                               'pair the alias audit needs is absent')
        if ctx.get('plant') is None:
            return case.finish('inconclusive', 'the run publishes '
                               'no plant endpoint for the claim ops')
        for action in ('start_driven', 'stop_driven',
                       'stop_controller', 'start_controller'):
            if ctx.get(action) is None:
                return case.finish('inconclusive', 'the run '
                                   'context carries no ' + action
                                   + ' action — the rig seam the '
                                   'alias staging needs is absent')
        if ctx.get('driven') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no driven endpoint — the '
                               'third controller has no monitor')
        journal_files = ctx.get('journal_files') or {}
        missing = [name for name in ('active', 'standby', 'driven')
                   if journal_files.get(name) is None]
        if missing:
            return case.finish('inconclusive', 'the run context '
                               'carries no journal files for '
                               + json.dumps(missing) + ' — the '
                               'durable half of the audit is absent')
        for name in ('active', 'standby'):
            try:
                _role(ctx, ctx[name])
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s '
                                   'monitor is unreachable: '
                                   + str(exc)[:200])
        deadline = time.monotonic() + ALIAS_SETTLE
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=ALIAS_POLL)
        if owner is None:
            return case.finish('failed', 'no peer reports '
                               'role=active')
        peer = 'standby' if owner == 'active' else 'active'
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=ALIAS_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the settle the '
                               'leg suspends inside was never '
                               'reached')
        # The contract surface: the owner's checkpoint must carry
        # the receipt window and admission counters the index
        # correlation reads, and the command path must echo the
        # declared actor/reason — the submission-record identity
        # the carry test compares.
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
                               'suspended-alias-audit contract')
        _, signals = http_json('GET', ctx[owner] + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'suspended-alias-signals.json', signals)
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
        # identity the contract compares — before any induction.
        status, probe = http_json(
            'POST', ctx[owner] + '/command',
            {'command': {'write_value': {
                'point': point, 'kind': 'bool',
                'value': {'bool': baseline}}},
             'actor': 'qa-alias-contract-probe',
             'reason': 'suspended-alias-audit-contract'})
        if status != 200 or _outcome_key(probe) != 'accepted':
            return case.finish('inconclusive', 'the contract probe '
                               'submission drew no admission: '
                               + str(status) + ' '
                               + json.dumps(probe)[:200])
        if probe.get('actor') != 'qa-alias-contract-probe' \
                or probe.get('reason') \
                != 'suspended-alias-audit-contract':
            return case.finish('inconclusive', 'the served receipt '
                               'drops the declared actor/reason — '
                               'the rig predates the '
                               'suspended-alias-audit contract')
        case.observe('field owner: ' + owner + ' (' + ctx[owner]
                     + '); sibling standby: ' + peer
                     + '; collision point ' + str(point))
        try:
            launched = ctx['start_driven'](owner)
        except Exception as exc:
            return case.finish('inconclusive', 'the driven-peer '
                               'launch never completed: '
                               + str(exc)[:300])
        case.observe('driven peer up on ' + owner + ': '
                     + str((launched or {}).get('container')))
        if wait_for(lambda: _try_role(ctx, ctx['driven']),
                    time.monotonic() + ALIAS_SETTLE,
                    interval=ALIAS_POLL) is None:
            return case.finish('inconclusive', 'the driven monitor '
                               'never answered /role')
        digests = []
        audits = []
        try:
            for number in (1, 2):
                digest, violations, evidence = _alias_pass(
                    ctx, number, owner, peer, point, value)
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'suspended-alias-pass-' + str(number) + '.json',
                    evidence)
                case.evidence('file', ref, 'suspended-alias pass '
                              + str(number) + ' — the driven '
                              'convergence, the stopped sibling, '
                              'the fenced demote\'s suspended '
                              'receipt, the identical admission\'s '
                              'own-index settle, the monitor and '
                              'durable journal audits, and the '
                              'normalized digest')
                if evidence.get('inconclusive'):
                    return case.finish(
                        'inconclusive',
                        evidence['inconclusive'])
                if violations or digest is None:
                    diagnostic = 'suspended-alias-audit-failed' \
                        if digest is None or any(
                            name == 'suspended-alias-audit-failed'
                            for name, _ in violations.values()) \
                        else 'suspended-alias-audit-' \
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
            # pass may have left the holder stopped, the rogue
            # claim standing, and the driven peer owning the field —
            # bring the holder back, demote whichever peer still
            # owns the field, promote the entry owner, and converge
            # — then the driven container is removed. A live rogue
            # claim fences every conditional promote, so a
            # still-tracking driven peer's promote — its claim
            # unconditional — is the wedge's only release.
            try:
                ctx['start_controller'](owner)
            except Exception:
                pass
            try:
                restored = False
                driven_lifted = False
                deadline = time.monotonic() + ALIAS_SETTLE
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
                        status, _ = _settle_call(ctx[owner]
                                                 + '/promote')
                        if status != 200 and not driven_lifted \
                                and _tracking(reports['driven']):
                            _settle_call(ctx['driven'] + '/promote')
                            driven_lifted = True
                    restored = _pair_active(ctx) == owner \
                        and _tracking_standby(ctx, peer) is not None
                    if not restored:
                        time.sleep(ALIAS_POLL)
            except Exception as exc:
                case.observe('cleanup: role restore failed: '
                             + str(exc)[:200])
            try:
                ctx['stop_driven']()
            except Exception:
                pass
        if digests[0] != digests[1]:
            return case.finish(
                'failed', 'suspended-alias-audit-nondeterministic: '
                'the two passes\' digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two suspended-alias passes, identical '
                     'digests')

        # The unchecked self-check: each audit clause, run over a
        # planted negative, must name its violation — a silent
        # audit can no longer be trusted to catch what it names.
        admissions = (audits[-1].get('admissions') or [])
        window = audits[-1].get('window') or {}
        if admissions:
            suspended = admissions[0]
            identical = admissions[1]
            planted = {}
            verdict = _suspended_verdict(
                [], [], suspended,
                lambda key, diagnostic, detail:
                    planted.setdefault(key, (diagnostic, detail)))
            if verdict != 'vanished' \
                    or planted.get('suspended-vanished', ('',))[0] \
                    != 'suspended-alias-audit-failed':
                return case.finish(
                    'failed', 'suspended-alias-audit-unchecked: '
                    'the suspended-admission audit stayed silent '
                    'on a planted vanished record')
            planted = {}
            _identical_verdict(
                {}, None, identical,
                lambda key, diagnostic, detail:
                    planted.setdefault(key, (diagnostic, detail)))
            if 'identical-index' not in planted:
                return case.finish(
                    'failed', 'suspended-alias-audit-unchecked: '
                    'the identical-admission audit stayed silent '
                    'on a planted wrong-index record')
            case.observe('the self-check leg\'s planted negatives '
                         'each reported their named diagnostic')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
