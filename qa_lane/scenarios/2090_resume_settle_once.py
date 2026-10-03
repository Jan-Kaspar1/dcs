"""The resume_settle_once acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the resume-settle leg shares the launch-layout window —
# it demotes, stops, and restarts both pair members through the
# runner's lifecycle seam and lands the pair back on the launch
# roles before the tune case's a->b switch.
RUNS_BEFORE = frozenset({'scenario_parameter_tune_carryover'})


# --------------------------------------------------------------------
# The settle-once-across-resume contract — the #1056 fix's rig
# evidence for WW-LCM-001's restart-resume clause and the
# receipt-as-truth rule, pinned distinctly from the restart leg's
# single-line resume integrity (0400) and the live-pair demote and
# carry boundaries (1800/1900): none of those restarts a quiesced
# peer into a run whose suspended copy a successor already applied.
# The finding's reproduction: a receipted command on the field owner
# rolls back to `Accepted` at the fenced demote — the suspended
# shape the demoted run's cycle-end checkpoint persists stamped
# `source_owns_field: false`; the promoted peer carries and applies
# it plus a newer command on the same point; restarting the quiesced
# peer as the field-owning active must not re-queue the restored
# receipt — on the defective build it re-applied the stale command,
# stomping the newer settle's value and minting a second terminal
# `command_settled` for one admission, while the rejoining peer's
# adopted document regressed its served verdict.
#
# Staging: a receipted admission lands on the launched-active
# owner, then a rogue `claim_writer` preempts its field claim — the
# fenced detection scan rolls the just-applied boundary back to
# `Accepted` and demotes the owner in place. While the monitor-less
# rogue claim stands the demoted peer can prove no tracking source —
# the claim declares no monitor and no announced hint names a field
# owner — so its suspended checkpoint never meets the covering
# adoption that would adjudicate it, and the runner's stop freezes
# exactly the pre-adoption document. The orphaned standby stays
# promotable through the dead source's missed pulls; its promote
# claims over the rogue, applies the carried admission and a newer
# command, then freezes the incumbent for the resumed peer's
# conditional startup grant — granted only once the dead owner's
# claim holds no live holder.

RESUME_SETTLE = 45    # bound on each demote/carry/promote/converge wait
RESUME_AUDIT = 30     # bound on the post-rejoin audit reads
RESUME_POLL = 0.4     # cadence on the pair posture waits
RESUME_WATCH = 0.05   # cadence on the demote and file watches
RESUME_ATTEMPTS = 3   # staged-admission bound — a claim that landed
                      # past the apply boundary re-stages on the
                      # re-taken field


def _promotable_standby(ctx, name):
    """The endpoint's report while it holds a promotable standby
    posture — tracking, orphaned, or reinitialized, the converged
    shapes `POST /promote` accepts — else None. The orphaned shape
    is this leg's carrier: a standby whose tracked source demoted in
    place reports orphaned and keeps reporting it through the dead
    source's missed pulls."""
    try:
        report = _role(ctx, ctx[name])
    except Exception:
        return None
    if report.get('role') == 'standby' \
            and set(report.get('sync') or {}) \
            & {'tracking', 'orphaned', 'reinitialized'}:
        return report
    return None


def _state_checkpoint(path):
    """The controller's persisted --state-file checkpoint as a dict,
    or None while the file is absent or mid-rewrite — a lost poll,
    never the leg's verdict."""
    try:
        document = json.loads(Path(path).read_text())
    except Exception:
        return None
    return document if isinstance(document, dict) else None


def _suspended_capture(path, admission):
    """The demoted owner's persisted document once it carries the
    suspended shape the resume exercises — the checkpoint stamped
    non-owning — with the admission's receipt as the file recorded
    it. Returns None while no non-owning document has landed; the
    caller judges the receipt's outcome (still `accepted` means the
    suspension froze, anything else means the apply beat the
    preemption and the stage must re-run)."""
    document = _state_checkpoint(path)
    if document is None or document.get('source_owns_field') is not False:
        return None
    receipt = next(
        (entry for entry in _receipt_list(document.get('receipts'))
         if _admission_hit(entry, admission)), None)
    return {'document': document, 'receipt': receipt}


def _served_receipt(ctx, base, admission):
    """The monitor's served receipt for the admission — matched on
    the (command, actor) submission identity — or None."""
    try:
        receipts, _ = _receipt_window(ctx, base)
    except Exception:
        return None
    return next((entry for entry in receipts
                 if _admission_hit(entry, admission)), None)


def _settled_receipts(ctx, base, admission):
    """The monitor's journaled `command_settled` receipts matching
    the admission — the serving half of the one-settle audit."""
    try:
        _, journal = http_json('GET', base + '/journal')
    except Exception:
        return None
    return [receipt for receipt in
            (_journal_settled(entry) for entry in _journal_list(journal))
            if _admission_hit(receipt, admission)]


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


def _finish_pass(evidence, violations):
    """Close a pass early — the digest is None where the stage or
    the contract never reached the audit."""
    evidence['digest'] = None
    return None, violations, evidence


def _resume_pass(ctx, number, owner, peer, point):
    """One resume-settle pass: admit a command on the field owner,
    preempt its claim so the fenced demote freezes the suspended
    receipt into the --state-file, carry and settle it plus a newer
    command on the orphaned standby, then restart the quiesced peer
    through the runner's lifecycle seam so it resumes as the
    field-owning active — and audit that the restored receipt parks
    instead of re-applying. Returns (digest, violations, evidence):
    the digest is the pass's normalized verdict record, identical
    across clean passes."""
    violations = {}
    evidence = {'pass': number, 'entry_owner': owner, 'point': point}

    def note(key, diagnostic, detail):
        violations.setdefault(key, (diagnostic, detail))

    def failed(key, detail):
        note(key, 'resume-settle-once-failed', detail)

    def inconclusive(key, detail):
        evidence['inconclusive'] = key + ': ' + detail

    owner_base, peer_base = ctx[owner], ctx[peer]
    state_path = ctx['state_files'][owner]
    journal_files = ctx['journal_files']

    # The journal floors the served audit reads from — a peer that
    # cannot serve its journal cannot evidence the contract.
    floors = {}
    try:
        for name in (owner, peer):
            _, journal = http_json('GET', ctx[name] + '/journal')
            entries = _journal_list(journal)
            floors[name] = (entries[-1].get('seq') or 0) \
                if entries else 0
    except Exception as exc:
        failed('floors', 'a peer\'s journal floor never served: '
               + str(exc)[:200])
        floors = None
    evidence['floors'] = floors

    # The two writes the stomp test separates: the carried command's
    # value must differ from the newer command's — the resumed peer
    # re-applying the stale one is what the defective image showed —
    # and the newer write lands the baseline back so the rolled-back
    # demotion image already equals the surviving verdict.
    snapshot = _try_snapshot(ctx, owner_base) or {}
    baseline = _point_value(snapshot, point)
    if not isinstance(baseline, bool):
        baseline = False
    first_value = not baseline
    second_value = baseline
    first = {'point': point, 'value': first_value,
             'command': {'write_value': {'point': point,
                                         'kind': 'bool',
                                         'value': {'bool':
                                                   first_value}}}}
    second = {'point': point, 'value': second_value,
              'command': {'write_value': {'point': point,
                                          'kind': 'bool',
                                          'value': {'bool':
                                                    second_value}}}}

    # ---- stage the suspended admission -----------------------------
    #
    # The suspension window: the admission must reach the fenced
    # scan still pending or applied at its head — either way
    # `suspend_boundary_commands` replays it back to `Accepted` and
    # the cycle-end persist freezes that shape. A claim landing only
    # after the applying scan settled for good leaves the receipt
    # `applied` on the demoted file — the stage missed, and the pair
    # re-takes the field for another admission.
    staged = None
    attempts = []
    for round_index in range(RESUME_ATTEMPTS):
        actor = 'qa-resume-' + str(number) + '-' + str(round_index)
        admission = {'actor': actor,
                     'command': first['command'],
                     'point': point,
                     'index': _next_receipt_index(ctx, owner_base),
                     'demoted': owner, 'promoted': peer}
        try:
            status, receipt = http_json(
                'POST', owner_base + '/command',
                {'command': first['command'], 'actor': actor,
                 'reason': 'resume-settle-once'})
        except Exception as exc:
            status, receipt = None, str(exc)
        attempt = {'actor': actor, 'status': status,
                   'receipt': receipt}
        if status == 200 and _outcome_key(receipt) == 'accepted':
            # The preemption the finding's demote hangs on: the
            # rogue claim preempts unconditionally and declares no
            # monitor, so the fenced demote the owner's next scan
            # runs leaves it no tracking source to prove.
            try:
                stream = _plant_connect(ctx)
                try:
                    preempt = _plant_request(
                        stream, {'op': 'claim_writer',
                                 'owner': CLAIM_ROGUE})
                finally:
                    stream.close()
            except Exception as exc:
                preempt = {'error': str(exc)[:200]}
            attempt['preempt'] = preempt
            if not isinstance(preempt, dict) \
                    or preempt.get('result') != 'done':
                inconclusive('preempt', 'the rogue claim answered '
                             + json.dumps(preempt)[:200]
                             + ' — the fenced demote never staged')
                attempts.append(attempt)
                evidence['attempts'] = attempts
                return None, violations, evidence
            # The demote watch: the fenced scan demotes in
            # place, the cycle-end persist stamps the checkpoint
            # non-owning — and while the rogue stands, no
            # covering adoption can reach the suspended file.
            # The probes ride the owner's own monitor reads, the
            # paced-rig seam every wait in the leg drives scans
            # through.
            def capture():
                _try_role(ctx, owner_base)
                return _suspended_capture(state_path, admission)

            deadline = time.monotonic() + RESUME_SETTLE
            captured = wait_for(capture, deadline,
                                interval=RESUME_WATCH)
            attempt['capture'] = {
                'owns_field': (captured or {}).get('document', {})
                .get('source_owns_field'),
                'receipt': (captured or {}).get('receipt')}
            if captured is None:
                inconclusive(
                    'demote', 'the fenced demote\'s non-owning '
                    'checkpoint never reached ' + owner
                    + '\'s state file — the rig predates the '
                    'suspended-resume contract')
                attempts.append(attempt)
                evidence['attempts'] = attempts
                return None, violations, evidence
            outcome = _outcome_key(captured['receipt'])
            if outcome == 'accepted':
                staged = admission
                attempts.append(attempt)
                break
            # The apply beat the preemption: the admission
            # settled on the owner's own line — a correct run,
            # just not the suspended shape. Re-take the field
            # for the next admission: the orphaned sibling can
            # promote over the rogue, the demoted peer resolves
            # the claimed monitor onto it, and a re-promote puts
            # the launch roles back.
            attempt['missed'] = 'settled-' + outcome
            recovered = None
            deadline = time.monotonic() + RESUME_SETTLE
            while recovered is None \
                    and time.monotonic() < deadline:
                if _pair_active(ctx) is None \
                        and _promotable_standby(ctx, peer) \
                        is not None:
                    _settle_call(peer_base + '/promote')
                owner_report = _try_role(ctx, owner_base)
                if (owner_report or {}).get('role') \
                        == 'standby' \
                        and _pair_active(ctx) == peer \
                        and _tracking_standby(ctx, owner) is not None:
                    _settle_call(owner_base + '/promote')
                if _pair_active(ctx) == owner \
                        and _tracking_standby(ctx, peer) \
                        is not None:
                    recovered = True
                else:
                    time.sleep(RESUME_POLL)
            attempt['recovered'] = recovered
            attempts.append(attempt)
            if not recovered:
                inconclusive(
                    'restage', 'the pair never re-took the '
                    'launch roles after the missed suspension '
                    'window — the demoted peer never reconverged')
                evidence['attempts'] = attempts
                return None, violations, evidence
            continue
        else:
            attempt['preempt'] = None
            if status == 200:
                inconclusive('admission', 'the staged submission '
                             'answered ' + json.dumps(receipt)[:200]
                             + ' — no admission to suspend')
            else:
                inconclusive('admission', 'the staged submission '
                             'drew ' + str(status) + ' '
                             + json.dumps(receipt)[:200])
            attempts.append(attempt)
            evidence['attempts'] = attempts
            return None, violations, evidence
    evidence['attempts'] = attempts
    if staged is None:
        inconclusive(
            'suspension', 'no admission froze suspended across '
            + str(RESUME_ATTEMPTS) + ' staged rounds — every '
            'preemption landed past the apply boundary')
        return None, violations, evidence
    evidence['admission'] = staged

    # The suspension's carry half: the orphaned sibling's pulls keep
    # adopting the demoted checkpoint — the suspended receipt lands
    # in its window still `Accepted`, and its posture stays
    # promotable through the stop that follows.
    deadline = time.monotonic() + RESUME_SETTLE

    def carry():
        receipt = _served_receipt(ctx, peer_base, staged)
        report = _promotable_standby(ctx, peer)
        if receipt is None or report is None:
            return None
        return {'receipt': receipt, 'report': report}

    carried = wait_for(carry, deadline, interval=RESUME_POLL)
    evidence['carried'] = carried
    if carried is None:
        inconclusive(
            'carry', 'the sibling never adopted the suspended '
            'admission into a promotable posture — the carry the '
            'resume leaves to the successor never formed')
        return None, violations, evidence

    # The freeze: the runner's stop on the demoted container holds
    # the suspended document — no covering adoption can reach it
    # now, and the rejoin never observed the line's adjudication.
    try:
        ctx['stop_controller'](owner)
    except Exception as exc:
        inconclusive('holder-stop', 'the runner\'s stop_controller '
                     'on ' + owner + ' never completed: '
                     + str(exc)[:200])
        return None, violations, evidence
    evidence['holder_stopped'] = True

    # The successor's promote: the orphaned-but-converged peer's
    # claim preempts the rogue token; its final-sync fetch meets the
    # stopped holder and carries nothing, so the settled line is the
    # tracking pull's own carry.
    promote_status, promoted = _settle_call(peer_base + '/promote')
    evidence['peer_promote'] = {'status': promote_status,
                                'body': promoted}
    if promote_status != 200:
        failed('promote', 'POST /promote on the orphaned sibling '
               'answered ' + str(promote_status) + ' '
               + json.dumps(promoted)[:200])
        return _finish_pass(evidence, violations)
    owned = wait_for(
        lambda: peer if _pair_active(ctx) == peer else None,
        time.monotonic() + RESUME_SETTLE, interval=RESUME_POLL)
    evidence['peer_owned'] = owned
    if owned is None:
        failed('peer-active', 'the promoted sibling never settled '
               'into the active role')
        return _finish_pass(evidence, violations)

    # The carried admission's one settlement: the promoted peer's
    # first field-owning boundary applies what the carry held.
    settled = wait_for(
        lambda: _served_receipt(ctx, peer_base, staged)
        if _outcome_key(_served_receipt(ctx, peer_base, staged)
                        or {}) != 'accepted' else None,
        time.monotonic() + RESUME_AUDIT, interval=RESUME_POLL)
    evidence['carried_settle'] = settled
    if settled is None or _outcome_key(settled) != 'applied':
        failed('carried-settle', 'the carried admission settled '
               + _outcome_key(settled) + ' on the successor — the '
               'suspended line owed exactly one applied')
        return _finish_pass(evidence, violations)

    # The newer command on the same point — the verdict the stale
    # re-apply would stomp.
    second['actor'] = 'qa-resume-' + str(number) + '-newer'
    second['index'] = _next_receipt_index(ctx, peer_base)
    second['demoted'] = owner
    second['promoted'] = peer
    try:
        status, receipt = http_json(
            'POST', peer_base + '/command',
            {'command': second['command'], 'actor': second['actor'],
             'reason': 'resume-settle-once'})
    except Exception as exc:
        status, receipt = None, str(exc)
    evidence['newer_submission'] = {'status': status,
                                    'receipt': receipt}
    if status != 200 or _outcome_key(receipt) != 'accepted':
        failed('newer-submit', 'the newer submission on the '
               'successor answered ' + str(status) + ' '
               + json.dumps(receipt)[:200])
        return _finish_pass(evidence, violations)
    newer = wait_for(
        lambda: _served_receipt(ctx, peer_base, second)
        if _outcome_key(_served_receipt(ctx, peer_base, second)
                        or {}) != 'accepted' else None,
        time.monotonic() + RESUME_AUDIT, interval=RESUME_POLL)
    evidence['newer_settle'] = newer
    if newer is None or _outcome_key(newer) != 'applied':
        failed('newer-settle', 'the newer command settled '
           + _outcome_key(newer) + ' on the successor — its apply '
           'boundary owed exactly one applied')
        return _finish_pass(evidence, violations)

    # The pre-resume truth: the surviving line's served verdicts for
    # both admissions, captured before the restart so the rejoin's
    # stability check compares the same records.
    before = {'suspended': _served_receipt(ctx, peer_base, staged),
              'newer': _served_receipt(ctx, peer_base, second)}
    evidence['before_restart'] = before

    # The incumbent freezes: the successor's stop leaves its claim
    # standing with no live holder — the dead-owner shape the
    # conditional startup grant preempts — and its own --state-file
    # carries the settled verdicts the rejoin resumes.
    try:
        ctx['stop_controller'](peer)
    except Exception as exc:
        inconclusive('peer-stop', 'the runner\'s stop_controller on '
                     + peer + ' never completed: ' + str(exc)[:200])
        return _finish_pass(evidence, violations)
    evidence['peer_stopped'] = True

    # The rig's container restart: the demoted run's suspended
    # checkpoint resumes restart-as-active — its restored `Accepted`
    # must park for the line's adjudication rather than re-queue.
    try:
        ctx['restart_controller'](owner)
    except Exception as exc:
        inconclusive('holder-restart', 'the runner\'s '
                     'restart_controller on ' + owner
                     + ' never completed: ' + str(exc)[:200])
        return _finish_pass(evidence, violations)
    resumed = None
    deadline = time.monotonic() + RESUME_SETTLE
    while resumed is None and time.monotonic() < deadline:
        report = _try_role(ctx, owner_base)
        if (report or {}).get('role') == 'active':
            resumed = report
        elif report is None:
            # The startup grant still fencing against a not-yet-
            # reaped incumbent fails the resumed process — one
            # re-start after the dead claim dropped its holder.
            try:
                ctx['restart_controller'](owner)
            except Exception:
                pass
            time.sleep(RESUME_POLL)
        else:
            time.sleep(RESUME_WATCH)
    evidence['resumed'] = resumed
    if resumed is None:
        inconclusive('resume-active', 'the restarted peer never '
                     'reported active — the conditional startup '
                     'grant never took the dead incumbent\'s field')
        return _finish_pass(evidence, violations)

    # The defect window: give the resumed run a few boundaries —
    # long enough for a re-queued stale command to re-apply and
    # journal — then read what it did with the restored receipt.
    deadline = time.monotonic() + RESUME_AUDIT / 3
    leaked = None
    while time.monotonic() < deadline and leaked is None:
        receipt = _served_receipt(ctx, owner_base, staged)
        settles = _settled_receipts(ctx, owner_base, staged)
        if (receipt is not None
                and _outcome_key(receipt) != 'accepted') or settles:
            leaked = {'receipt': receipt, 'settles': settles}
        else:
            time.sleep(RESUME_POLL)
    evidence['leak_window'] = leaked
    suspended_after = _served_receipt(ctx, owner_base, staged)
    evidence['resumed_receipt'] = suspended_after
    resumed_snapshot = _try_snapshot(ctx, owner_base) or {}
    evidence['resumed_image'] = _point_value(resumed_snapshot, point)

    # The incumbent rejoins: its own persisted run resumes — applied
    # verdicts and image included — and tracks the restarted peer's
    # line. The adopted still-`Accepted` view of the admission must
    # not regress the run's own settled verdict.
    try:
        ctx['restart_controller'](peer)
    except Exception as exc:
        inconclusive('peer-restart', 'the runner\'s '
                     'restart_controller on ' + peer
                     + ' never completed: ' + str(exc)[:200])
        return _finish_pass(evidence, violations)
    rejoined = wait_for(
        lambda: _tracking_standby(ctx, peer),
        time.monotonic() + RESUME_SETTLE, interval=RESUME_POLL)
    evidence['rejoined'] = rejoined
    if rejoined is None:
        failed('rejoin', 'the restarted incumbent never '
               'reconverged tracking behind the resumed peer')
        return _finish_pass(evidence, violations)

    # ---- the audit ------------------------------------------------
    #
    # Every clause reads the pair's served surfaces or the durable
    # journal files — the externally visible shape of the contract,
    # never the private mark the runtime parks the receipt under.
    after = {'suspended': _served_receipt(ctx, peer_base, staged),
             'newer': _served_receipt(ctx, peer_base, second)}
    evidence['after_rejoin'] = after
    images = {name: _point_value(_try_snapshot(ctx, ctx[name]) or {},
                                 point)
              for name in (owner, peer)}
    evidence['images'] = images
    journaled = {name: _settled_receipts(ctx, ctx[name], staged)
                 for name in (owner, peer)}
    journaled_newer = {name: _settled_receipts(ctx, ctx[name], second)
                       for name in (owner, peer)}
    evidence['journaled'] = {'suspended': journaled,
                             'newer': journaled_newer}
    durable = {name: _file_settles(journal_files[name], staged)
               for name in (owner, peer)}
    durable_newer = {name: _file_settles(journal_files[name], second)
                     for name in (owner, peer)}
    evidence['durable'] = {'suspended': durable,
                           'newer': durable_newer}

    digest = {}
    # The point keeps the newer command's value on both monitors —
    # the stale re-apply's stomp is the defective image's only
    # signature.
    stomped = [name for name, value in images.items()
               if value != second_value]
    if stomped:
        failed('point-value', 'the point serves '
               + json.dumps(images) + ' — not the newer command\'s '
               'value ' + json.dumps(second_value) + '; the '
               'restarted run\'s stale apply shows on '
               + json.dumps(stomped))
        digest['point'] = 'stomped'
    else:
        digest['point'] = 'newer'

    # The restored receipt stays honestly `Accepted` on the resumed
    # peer — parked, never re-queued.
    if suspended_after is None:
        failed('suspended-missing', 'the restarted peer\'s log lost '
               'the suspended admission outright')
        digest['resumed'] = 'vanished'
    elif _outcome_key(suspended_after) == 'accepted':
        digest['resumed'] = 'parked'
    else:
        failed('resumed-verdict', 'the restarted peer\'s receipt '
               'settled ' + _outcome_key(suspended_after)
               + ' — the restored admission re-queued instead of '
               'parking')
        digest['resumed'] = 'reapplied'

    # The rejoining peer's served verdicts are the pre-restart
    # records — never regressed to the adopted `Accepted`, never
    # rewritten to a restarted run's settle.
    for key, admission in (('suspended', staged), ('newer', second)):
        pre, post = before[key], after[key]
        if pre is None or post is None:
            failed('verdict-' + key, 'the rejoined peer\'s log lost '
                   'the ' + key + ' admission\'s receipt')
            digest[key + '_verdict'] = 'lost'
        elif post.get('outcome') != pre.get('outcome'):
            failed('verdict-' + key, 'the ' + key
                   + ' admission\'s served verdict mutated across '
                   'the rejoin: ' + json.dumps(pre.get('outcome'))
                   + ' -> ' + json.dumps(post.get('outcome')))
            digest[key + '_verdict'] = 'mutated'
        else:
            digest[key + '_verdict'] = 'stable'

    # One admission, one terminal settlement across the pair's
    # journals — served and durable alike: the suspended admission's
    # single applied lives on the successor's record alone; the
    # resumed peer owes it none.
    counts = {'suspended': {}, 'newer': {}}
    for key, served, files in (
            ('suspended', journaled, durable),
            ('newer', journaled_newer, durable_newer)):
        for name in (owner, peer):
            seen = served.get(name)
            filed = files.get(name)
            counts[key][name] = {
                'served': None if seen is None else len(seen),
                'durable': None if filed is None else len(filed),
                'outcomes': [_outcome_key(r) for r in (seen or [])]}
            expected = 1 if name == peer else 0
            served_count = None if seen is None else len(seen)
            filed_count = None if filed is None else len(filed)
            if served_count is None or filed_count is None:
                note('journal-' + key + '-' + name,
                     'resume-settle-once-nondeterministic',
                     name + '\'s ' + key + ' admission\'s settle '
                     'count never read — served '
                     + str(served_count) + ', durable '
                     + str(filed_count))
                continue
            if served_count != expected \
                    or filed_count != expected:
                diagnostic = 'resume-settle-once-failed' \
                    if expected == 1 and (served_count == 0
                                          or filed_count == 0) \
                    else 'resume-settle-once-nondeterministic'
                note('journal-' + key + '-' + name, diagnostic,
                     name + ' journaled ' + str(served_count)
                     + ' command_settled records (durable '
                     + str(filed_count) + ') for the ' + key
                     + ' admission — the contract owes exactly '
                     + str(expected) + ' there')
                continue
            if expected == 1 and (
                    _outcome_key(seen[0]) != 'applied'
                    or _outcome_key(filed[0]) != 'applied'):
                note('journal-' + key + '-' + name,
                     'resume-settle-once-nondeterministic',
                     name + '\'s ' + key + ' admission settled '
                     + _outcome_key(seen[0]) + ' served / '
                     + _outcome_key(filed[0]) + ' durable — the '
                     'line\'s one settlement is applied')
    digest['journals'] = 'once-each' if not any(
        key.startswith('journal-') for key in violations) else 'split'
    evidence['counts'] = counts

    # The launch roles: the resumed owner holds the field, the
    # rejoined incumbent tracks it — the layout the cases behind
    # this one enter on.
    restored = _pair_active(ctx) == owner \
        and _tracking_standby(ctx, peer) is not None
    if not restored:
        failed('roles', 'the pair did not land back on the launch '
               'roles — owner ' + str(_pair_active(ctx)))
        digest['roles'] = 'unrestored'
    else:
        digest['roles'] = 'restored'
    evidence['digest'] = digest
    return digest, violations, evidence


def scenario_resume_settle_once(ctx):
    """Exercise the settle-once-across-resume contract on the
    deployed pair: a receipted command suspended at the fenced
    demote and frozen into the quiesced peer's --state-file must
    never re-apply when that peer restarts as the field-owning
    active — the successor's carried settle is the admission's one
    terminal settlement, the point keeps the newer command's value,
    and the rejoining peer's served verdict stays the truth."""
    case = Case(
        'resume-settle-once',
        'A resumed demotion cannot re-apply the carried command',
        'with the deployed pair settled and tracking, a receipted '
        'writable-point command on the field owner rolls back to '
        'Accepted at a rogue claim\'s fenced demote and freezes '
        'into the demoted run\'s --state-file checkpoint while no '
        'tracking source can be proved; the orphaned sibling '
        'promotes over the rogue, applies the carried admission '
        'and a newer command on the same point, then yields the '
        'field to the restarted quiesced peer — which resumes '
        'restart-as-active with the suspended receipt parked '
        'rather than re-applied: through both serving monitors and '
        'both durable journals the point serves the newer '
        'command\'s value, each admission carries exactly one '
        'command_settled, and the rejoining peer\'s settled '
        'verdict stays the served truth; two passes produce '
        'identical digests and the launch roles restore')
    owner, peer = 'active', 'standby'
    try:
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries only one endpoint — the '
                               'pair the resume leg needs is absent')
        if ctx.get('plant') is None:
            return case.finish('inconclusive', 'the run publishes '
                               'no plant endpoint for the claim ops')
        missing = [action for action in
                   ('stop_controller', 'start_controller',
                    'restart_controller')
                   if ctx.get(action) is None]
        if missing:
            return case.finish('inconclusive', 'the run context '
                               'carries no ' + json.dumps(missing)
                               + ' action — the runner lifecycle '
                               'seam the resume staging needs is '
                               'absent')
        state_files = ctx.get('state_files') or {}
        journal_files = ctx.get('journal_files') or {}
        for name in (owner, peer):
            if state_files.get(name) is None:
                return case.finish('inconclusive', 'the run context '
                                   'carries no state file for '
                                   + name + ' — the suspended '
                                   'capture the resume reads is '
                                   'absent')
            if journal_files.get(name) is None:
                return case.finish('inconclusive', 'the run context '
                                   'carries no journal file for '
                                   + name + ' — the durable half of '
                                   'the settle audit is absent')
        for name in (owner, peer):
            try:
                _role(ctx, ctx[name])
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s '
                                   'monitor is unreachable: '
                                   + str(exc)[:200])
        deadline = time.monotonic() + RESUME_SETTLE
        active = wait_for(lambda: _pair_active(ctx), deadline,
                          interval=RESUME_POLL)
        if active is None:
            return case.finish('failed', 'no peer reports '
                               'role=active')
        if active != owner:
            return case.finish('inconclusive', 'the field owner is '
                               + active + ' — the resume exercise '
                               'needs the launched-active peer '
                               'owning the field; the pair\'s '
                               'layout predates the stage')
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=RESUME_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the settle the '
                               'leg suspends inside was never '
                               'reached')
        # The contract surface: the owner's checkpoint must carry
        # the receipt window and admission counters the admission
        # correlation reads, and the ownership stamp the suspended
        # document's resume gate checks.
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
                    .get('attempts'), int) \
                or 'source_owns_field' not in checkpoint:
            return case.finish('inconclusive', 'the served '
                               'checkpoint carries no receipt '
                               'window, admission counters, or '
                               'ownership stamp — the rig predates '
                               'the resume-settle contract')
        _, signals = http_json('GET', ctx[owner] + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'resume-settle-signals.json', signals)
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
        # declared actor and reason — the submission-record identity
        # the settle audit correlates by — before any induction.
        status, probe = http_json(
            'POST', ctx[owner] + '/command',
            {'command': {'write_value': {
                'point': point, 'kind': 'bool',
                'value': {'bool': baseline}}},
             'actor': 'qa-resume-contract-probe',
             'reason': 'resume-settle-once-contract'})
        if status != 200 or _outcome_key(probe) != 'accepted':
            return case.finish('inconclusive', 'the contract probe '
                               'submission drew no admission: '
                               + str(status) + ' '
                               + json.dumps(probe)[:200])
        if probe.get('actor') != 'qa-resume-contract-probe' \
                or probe.get('reason') \
                != 'resume-settle-once-contract':
            return case.finish('inconclusive', 'the served receipt '
                               'drops the declared actor/reason — '
                               'the rig predates the '
                               'resume-settle contract')
        case.observe('field owner: ' + owner + ' (' + ctx[owner]
                     + '); sibling standby: ' + peer
                     + '; resume point ' + str(point))
        digests = []
        try:
            for number in (1, 2):
                digest, violations, evidence = _resume_pass(
                    ctx, number, owner, peer, point)
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'resume-settle-pass-' + str(number) + '.json',
                    evidence)
                case.evidence('file', ref, 'resume-settle pass '
                              + str(number) + ' — the staged '
                              'suspension, the successor\'s '
                              'carried and newer settles, the '
                              'restart, the monitor and durable '
                              'journal audits, and the normalized '
                              'digest')
                if evidence.get('inconclusive'):
                    return case.finish(
                        'inconclusive', evidence['inconclusive'])
                if violations or digest is None:
                    diagnostic = 'resume-settle-once-failed' \
                        if digest is None or any(
                            name == 'resume-settle-once-failed'
                            for name, _ in violations.values()) \
                        else 'resume-settle-once-nondeterministic'
                    return case.finish(
                        'failed', diagnostic + ': ' + '; '.join(
                            detail for _, detail in
                            list(violations.values())[:4]))
                digests.append(digest)
        finally:
            # The launch layout for the cases behind this one: a
            # clean pass restores it by construction; an aborted
            # pass may have left either container stopped, the rogue
            # claim standing, and the sibling owning the field —
            # bring both peers back, demote whichever still owns
            # the field, promote the entry owner, and reconverge.
            try:
                restored = False
                deadline = time.monotonic() + RESUME_SETTLE
                while not restored \
                        and time.monotonic() < deadline:
                    # A peer whose monitor never answers is down —
                    # resume it from its state file; the launched
                    # active's startup grant preempts only once the
                    # standing claim's holder is dead.
                    for name in (owner, peer):
                        if _try_role(ctx, ctx[name]) is None:
                            try:
                                ctx['start_controller'](name)
                            except Exception:
                                pass
                    current = _pair_active(ctx)
                    if current is None:
                        for name in (peer, owner):
                            if _promotable_standby(ctx, name) \
                                    is not None:
                                _settle_call(ctx[name] + '/promote')
                                break
                    elif current != owner:
                        # The survivable answer for a wrong owner:
                        # demoting frees the claim so the launched
                        # active's next start takes the field.
                        _settle_call(ctx[current] + '/demote')
                        if _tracking_standby(ctx, owner) is not None:
                            _settle_call(ctx[owner] + '/promote')
                    restored = _pair_active(ctx) == owner \
                        and _tracking_standby(ctx, peer) is not None
                    if not restored:
                        time.sleep(RESUME_POLL)
                if not restored:
                    case.observe('cleanup: the pair did not settle '
                                 'back to the launch roles')
            except Exception as exc:
                case.observe('cleanup: role restore failed: '
                             + str(exc)[:200])
        if digests[0] != digests[1]:
            return case.finish(
                'failed', 'resume-settle-once-nondeterministic: '
                'the two passes\' digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two resume-settle passes, identical digests')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
