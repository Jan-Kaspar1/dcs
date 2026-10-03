"""The demote_forged_standby_source leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the leg shares the launch-layout window behind
# peer_announce — it needs the settled tracking pair, opens its own
# announced-only demotion window per pass (the tracking peer stopped,
# the field owner warm-restarted so no genuine announce stands), and
# restores the launch roles before the tune case's a->b switch.
RUNS_BEFORE = frozenset({'scenario_parameter_tune_carryover'})


# --------------------------------------------------------------------
# The announced-source demote-verify contract — the landed #850/#863
# behavior pinned as per-run lane evidence for WW-LCM-001's
# takeover-continuity clause and WW-FND-004's command integrity: a
# field owner that never configured a peer demotes only toward a
# tracking source some peer announced through its own
# `GET /checkpoint?peer=` pulls — and on a keyed run the demotion
# verifies that hint by pulling the announced endpoint and auditing
# the document against the owner's own command record: the answer
# must carry the keyed `line_proof` binding the pull's nonce to the
# document, and the document's receipt window and internal `In`
# samples must agree with the owner's settled log and held image. A
# forged standby-shaped document — a receipt window that forks the
# settled log, or an internal `In` sample planting a value no settled
# verdict produced — refuses `no_tracking_source` instead of
# redirecting the demoted peer's tracking onto forged state.
#
# The leg runs the hostile endpoint the verify dials as the run's
# bridge-placed forge (the endpoint_placement 'forge' record): a
# labeled rig-network container serving a staged checkpoint document
# and announcing itself to the owner's monitor. Each pass stops the
# tracking peer and warm-restarts the owner so the only announced
# hint is the forge's, submits one settled write whose receipt and
# held value the forgeries contradict, then exercises four legs
# against `POST /demote`: the tokenless forge serving the honest
# document (no valid line_proof — the keyed pair's proof gate
# refuses), the keyed forge serving the receipt-fork and the
# planted-internal forgeries (genuinely signed — the command-record
# audit convicts them), and finally the keyed forge serving the
# honest standby-shaped document, which must verify, journal its
# adoption, and let the demoted peer reconverge and re-promote — the
# refusal must not close the legitimate follow-peer path. Named
# diagnostics demote-forged-standby-failed for a contract miss and
# demote-forged-standby-nondeterministic when two passes disagree.

DEMOTE_FORGED_SETTLE = 45    # bound on each restart/switch and the
                             # pair's reconvergence
DEMOTE_FORGED_POLL = 0.4     # wait cadence inside the leg
FORGE_ANNOUNCE_SETTLE = 20   # bound on the forge's announce landing


def _forge_hits(path):
    """The parsed records of the forge endpoint's hits ledger —
    `{'kind': 'serve'|'announce', ...}` JSONL lines the bind-mounted
    file carries. A missing file or a torn final line reads as absent
    records — a lost observation, never the leg's verdict."""
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _forge_announced(forge):
    """True once the forge's hits ledger holds an announce pull the
    owner answered — the recorded hint POST /demote then verifies."""
    return any(record.get('kind') == 'announce' and record.get('ok')
               for record in _forge_hits(forge['hits']))


def _stage_document(forge, document):
    """Rewrite the forge's staged checkpoint document — the atomic
    rename lands the next shape without a relaunch or a torn read."""
    staged = Path(forge['dir']) / 'checkpoint.staging.json'
    staged.write_text(json.dumps(document))
    staged.replace(forge['document'])


def _standby_document(own):
    """The honest standby-shaped document derived from the field
    owner's served checkpoint: the same line's continuation stamped
    `source_owns_field: false` — the shape a real tracking peer's
    checkpoint carries — with the owner-hint and proof decorations
    stripped."""
    document = json.loads(json.dumps(own))
    document['source_owns_field'] = False
    document.pop('line_owner', None)
    document.pop('line_proof', None)
    return document


def _forked_document(own, index, point, value):
    """The receipt-window forgery: the honest standby document whose
    settled log claims a different command at this pass's settled
    submission index — the write's value flipped. Returns None when
    the owner's checkpoint no longer carries the index the forgery
    needs."""
    document = _standby_document(own)
    receipts = document.get('receipts')
    attempts = (document.get('command_admission') or {}).get('attempts')
    if not isinstance(receipts, list) \
            or not isinstance(attempts, int) \
            or isinstance(attempts, bool):
        return None
    position = index - (attempts - len(receipts))
    if not 0 <= position < len(receipts):
        return None
    forged = json.loads(json.dumps(receipts[position]))
    forged['command'] = {'write_value': {
        'point': point, 'kind': 'bool', 'value': {'bool': value}}}
    receipts[position] = forged
    return document


def _planted_document(own, point):
    """The planted-internal forgery: the honest standby document whose
    internal `In` image claims a held value no settled verdict
    produced — the target point's sample flipped from what the
    owner's own image and receipted write hold. Returns None when the
    checkpoint serves no bool sample at the point."""
    document = _standby_document(own)
    sample = (document.get('internal') or {}).get(str(point))
    if not isinstance(sample, dict):
        return None
    value = sample.get('value')
    if not isinstance(value, dict) \
            or not isinstance(value.get('bool'), bool):
        return None
    sample['value'] = {'bool': not value['bool']}
    return document


def _journaled_switch(ctx, owner, floor):
    """The owner journal's tail since `floor`: (tracking-source
    adoptions, role changes) — the durable record a refused demotion
    must not grow."""
    entries = _journal_entries(ctx['journal_files'][owner])
    adoptions, changes = [], []
    for item in entries[floor:]:
        event = (item.get('entry') or {}).get('event') or {}
        adopted = event.get('tracking_source_adopted')
        if isinstance(adopted, dict):
            adoptions.append(adopted)
        change = event.get('role_changed')
        if isinstance(change, dict):
            changes.append(change)
    return adoptions, changes


def _following_standby(ctx, name):
    """The endpoint's report while it is a standby converged onto the
    announced line — tracking, reinitialized, or orphaned (the honest
    document claims no field owner, so the demoted peer's convergence
    is the named orphaned shape — still promotable) — else None."""
    try:
        report = _role(ctx, ctx[name])
    except Exception:
        return None
    sync = report.get('sync')
    if report.get('role') == 'standby' and isinstance(sync, dict) \
            and {'tracking', 'orphaned', 'reinitialized'} & set(sync):
        return report
    return None


def _refusal_leg(ctx, base, owner, floor, forge, expect_signed):
    """One forged-document demote leg: POST /demote on the field owner
    while the forge serves its staged document. The contract: the call
    settles `409 no_tracking_source`, the journal since `floor` holds
    no tracking-source adoption and no role change, the peer still
    reports field owner, and the forge's hits ledger proves the verify
    pull reached it — signed exactly when `expect_signed` says the
    endpoint held the pair's token. Returns (record, problems)."""
    hits_before = len(_forge_hits(forge['hits']))
    status, body = _settle_call(base + '/demote')
    pulls = [record for record in _forge_hits(forge['hits'])[hits_before:]
             if record.get('kind') == 'serve']
    role = _try_role(ctx, base)
    adoptions, changes = _journaled_switch(ctx, owner, floor)
    record = {'demote': {'status': status, 'body': body},
              'verify_pulls': pulls, 'role_after': role,
              'journaled_adoptions': adoptions,
              'journaled_role_changes': changes}
    problems = []
    if status != 409 or body != 'no_tracking_source':
        problems.append('POST /demote answered ' + str(status) + ' '
                        + json.dumps(body)[:200]
                        + ' instead of settling no_tracking_source')
    if adoptions:
        problems.append('the refused demote journaled a '
                        'tracking-source adoption: '
                        + json.dumps(adoptions)[:200])
    if changes:
        problems.append('the refused demote journaled a role change: '
                        + json.dumps(changes)[:200])
    if (role or {}).get('role') != 'active':
        problems.append('the peer stopped owning the field on the '
                        'refused demote: ' + json.dumps(role)[:200])
    if not pulls:
        problems.append('the forge ledgered no checkpoint pull — the '
                        'refusal never reached the staged document')
    elif expect_signed \
            and not any(pull.get('signed') for pull in pulls):
        problems.append('no verify pull was answered signed — the '
                        'keyed document never reached the audit')
    elif not expect_signed \
            and any(pull.get('signed') for pull in pulls):
        problems.append('the tokenless endpoint answered signed — the '
                        'unproven leg never exercised the proof gate')
    return record, problems


def _demote_forged_pass(ctx, number, owner, point, baseline):
    """One forged-demote pass: the announced-only window, the pass's
    settled command, the unproven/forked/planted refusals, and the
    honest adoption plus reconvergence back to the entry roles.
    Returns (digest, violations, evidence): digest is the pass's
    normalized verdict record, identical across clean passes;
    violations is {key: (diagnostic, detail)} in first-seen order."""
    violations = {}
    evidence = {'entry_owner': owner, 'pass': number}
    digest = {'unproven': 'adopted', 'receipt_fork': 'adopted',
              'planted_internal': 'adopted', 'honest': 'refused',
              'roles': 'unchanged'}

    def note(key, diagnostic, detail):
        violations.setdefault(key, (diagnostic, detail))

    def failed(key, detail):
        note(key, 'demote-forged-standby-failed', detail)

    peer = 'standby' if owner == 'active' else 'active'
    base, peer_base = ctx[owner], ctx[peer]
    evidence.update({'owner': owner, 'peer': peer})

    # The settled gate: the entry owner holds the field and the other
    # launched peer tracks it — the pass's restore lands the same.
    if _pair_active(ctx) != owner \
            or _tracking_standby(ctx, peer) is None:
        failed('settle', 'the pair never settled — ' + owner
               + ' holds no active role with ' + peer
               + ' tracking behind it')
        return None, violations, evidence

    # The pass's settled command: the receipt the fork forges against
    # and the held value the planted document contradicts.
    index = _next_receipt_index(ctx, base)
    command = {'command': {'write_value': {
        'point': point, 'kind': 'bool', 'value': {'bool': not baseline}}},
        'actor': 'qa-lane-demote-forged-' + str(number)}
    try:
        command_status, receipt = http_json(
            'POST', base + '/command', command)
    except Exception as exc:
        failed('command', 'the field owner never answered the pass '
               'command: ' + str(exc)[:200])
        return None, violations, evidence
    settled = wait_for(
        lambda: _settled_outcome(ctx, base, index),
        time.monotonic() + DEMOTE_FORGED_SETTLE,
        interval=DEMOTE_FORGED_POLL)
    evidence['command'] = {'index': index, 'status': command_status,
                           'receipt': receipt, 'settled': settled}
    if settled != 'applied':
        failed('command', 'the pass command never settled applied — '
               'the forgeries have no settled receipt or held value '
               'to contradict: ' + json.dumps(receipt)[:200])
        return None, violations, evidence

    # The announced-only window: the tracking peer stopped, the owner
    # warm-restarted so its announced set begins empty — the forge's
    # announce below is then the only hint the demote verifies.
    try:
        ctx['stop_controller'](peer)
        ctx['restart_controller'](owner)
    except Exception as exc:
        failed('window', 'the lifecycle actions never opened the '
               'announced-only window: ' + str(exc)[:200])
        return None, violations, evidence
    restored = wait_for(
        lambda: ((_try_role(ctx, base) or {}).get('role') == 'active'
                 and _try_role(ctx, base)) or None,
        time.monotonic() + DEMOTE_FORGED_SETTLE,
        interval=DEMOTE_FORGED_POLL)
    evidence['window'] = {'peer': 'stopped',
                          'owner_restart': restored}
    if restored is None:
        failed('window', 'the restarted owner never reported active '
               'again')
        return None, violations, evidence

    # The journal floor: every record after it is this pass's — a
    # refused demote must add none of the adoption or role entries.
    floor = len(_journal_entries(ctx['journal_files'][owner]))

    # The owner's post-restart checkpoint — the document the honest
    # and forged shapes derive from.
    try:
        _, own = http_json('GET', base + '/checkpoint')
    except Exception as exc:
        failed('checkpoint', 'the restarted owner\'s checkpoint '
               'never answered: ' + str(exc)[:200])
        return None, violations, evidence
    if not isinstance(own, dict) \
            or own.get('source_owns_field') is not True \
            or not isinstance(own.get('tick'), int):
        failed('checkpoint', 'the restarted owner serves no '
               'field-owning checkpoint document: '
               + json.dumps(own)[:200])
        return None, violations, evidence
    evidence['own'] = {'tick': own['tick'],
                       'generation': own.get('generation'),
                       'attempts': (own.get('command_admission')
                                    or {}).get('attempts')}

    # The staged documents: the honest standby shape, the
    # receipt-window fork at this pass's settled index, and the
    # planted internal `In` sample.
    honest = _standby_document(own)
    forked = _forked_document(own, index, point, baseline)
    planted = _planted_document(own, point)
    evidence['documents'] = {
        'honest': 'standby-shaped continuation',
        'receipt_fork': None if forked is None else
        'receipts[%d].command claims the flipped write' % index,
        'planted_internal': None if planted is None else
        'internal[%d] plants %s' % (point, json.dumps(
            (planted['internal'][str(point)] or {}).get('value')))}
    if forked is None or planted is None:
        failed('documents', 'the served checkpoint cannot stage the '
               'forgeries: ' + json.dumps(evidence['documents'])[:300])
        return None, violations, evidence

    legs = {}
    evidence['legs'] = legs
    forge = None
    try:
        # The unproven leg: the tokenless endpoint serves the honest
        # document — no valid line_proof answers the keyed pull.
        forge = ctx['start_forge'](honest, owner, keyed=False)
        if wait_for(lambda: _forge_announced(forge) or None,
                    time.monotonic() + FORGE_ANNOUNCE_SETTLE,
                    interval=DEMOTE_FORGED_POLL) is None:
            failed('forge-announce', 'the unkeyed forge\'s announce '
                   'never landed on the owner — its hint was never '
                   'recorded')
            return None, violations, evidence
        record, problems = _refusal_leg(ctx, base, owner, floor,
                                      forge, expect_signed=False)
        legs['unproven'] = record
        if not problems:
            digest['unproven'] = 'refused'
        for detail in problems:
            failed('unproven', detail)
        ctx['stop_forge']()
        forge = None

        # The keyed legs: the token-holding endpoint signs every
        # answer, so only the staged document's content can convict
        # it — the receipt fork first, then the planted internal.
        forge = ctx['start_forge'](forked, owner, keyed=True)
        if wait_for(lambda: _forge_announced(forge) or None,
                    time.monotonic() + FORGE_ANNOUNCE_SETTLE,
                    interval=DEMOTE_FORGED_POLL) is None:
            failed('forge-announce', 'the keyed forge\'s announce '
                   'never landed on the owner — its hint was never '
                   'recorded')
            return None, violations, evidence
        record, problems = _refusal_leg(ctx, base, owner, floor,
                                      forge, expect_signed=True)
        legs['receipt_fork'] = record
        if not problems:
            digest['receipt_fork'] = 'refused'
        for detail in problems:
            failed('receipt-fork', detail)

        _stage_document(forge, planted)
        record, problems = _refusal_leg(ctx, base, owner, floor,
                                      forge, expect_signed=True)
        legs['planted_internal'] = record
        if not problems:
            digest['planted_internal'] = 'refused'
        for detail in problems:
            failed('planted-internal', detail)

        # The honest leg: the same keyed endpoint now serves the
        # standby-shaped continuation — the demotion must verify and
        # adopt it, journal the adoption naming the forge's address,
        # and the demoted peer must reconverge and re-promote.
        _stage_document(forge, honest)
        hits_before = len(_forge_hits(forge['hits']))
        demote_status, demote = _settle_call(base + '/demote')
        pulls = [record for record
                 in _forge_hits(forge['hits'])[hits_before:]
                 if record.get('kind') == 'serve']
        role_after = _try_role(ctx, base)
        adoptions, changes = _journaled_switch(ctx, owner, floor)
        legs['honest'] = {
            'demote': {'status': demote_status, 'body': demote},
            'verify_pulls': pulls, 'role_after': role_after,
            'journaled_adoptions': adoptions,
            'journaled_role_changes': changes}
        if demote_status != 200:
            failed('honest', 'the honest announced document was '
                   'refused — the legitimate follow-peer path is '
                   'closed: ' + str(demote_status) + ' '
                   + json.dumps(demote)[:200])
        elif len(adoptions) != 1:
            failed('honest', 'the honest demote journaled '
                   + str(len(adoptions)) + ' tracking-source '
                   'adoptions instead of one naming the forge')
        elif not str(adoptions[0].get('source', '')) \
                .endswith(':' + str(forge['port'])):
            failed('honest', 'the journaled adoption names '
                   + json.dumps(adoptions[0])[:200]
                   + ' instead of the forge endpoint')
        else:
            converged = wait_for(
                lambda: _following_standby(ctx, owner),
                time.monotonic() + DEMOTE_FORGED_SETTLE,
                interval=DEMOTE_FORGED_POLL)
            legs['honest']['reconverged'] = converged
            if converged is None:
                failed('reconverge', 'the demoted peer never '
                       'reconverged onto the adopted announced '
                       'source: ' + json.dumps(role_after)[:200])
            else:
                promoted, last = None, None
                deadline = time.monotonic() + DEMOTE_FORGED_SETTLE
                while time.monotonic() < deadline and promoted is None:
                    promote_status, body = _settle_call(
                        base + '/promote')
                    if promote_status == 200:
                        promoted = body
                    else:
                        last = (promote_status, body)
                        time.sleep(DEMOTE_FORGED_POLL)
                legs['honest']['promote'] = {
                    'body': promoted, 'last_refusal': last}
                if promoted is None:
                    failed('promote', 'the reconverged peer\'s '
                           'promote never succeeded: '
                           + json.dumps(last)[:300])
                elif wait_for(
                        lambda: (_pair_active(ctx) == owner or None)
                        and _try_role(ctx, base),
                        time.monotonic() + DEMOTE_FORGED_SETTLE,
                        interval=DEMOTE_FORGED_POLL) is None:
                    failed('promote', 'the promoted peer never '
                           'reported active again')
                else:
                    digest['honest'] = 'adopted'
    finally:
        if forge is not None:
            try:
                ctx['stop_forge']()
            except Exception:
                pass

    # The restore: the tracking peer's container back up — its
    # configured standby pull re-announces and re-tracks the owner —
    # so the next pass and the cases behind this one meet the launch
    # layout again.
    try:
        ctx['start_controller'](peer)
    except Exception as exc:
        failed('restore', 'the stopped peer never came back: '
               + str(exc)[:200])
        return None, violations, evidence
    settled = wait_for(
        lambda: (_pair_active(ctx) == owner or None)
        and _tracking_standby(ctx, peer),
        time.monotonic() + DEMOTE_FORGED_SETTLE,
        interval=DEMOTE_FORGED_POLL)
    evidence['restored'] = settled
    if settled is None:
        failed('restore', 'the pair never settled back to its entry '
               'role layout')
    else:
        digest['roles'] = 'restored'
    evidence['digest'] = dict(digest)
    evidence['violations'] = {key: diagnostic
                              for key, (diagnostic, _)
                              in violations.items()}
    return digest, violations, evidence


def scenario_demote_forged_standby_source(ctx):
    """Exercise the announced-source demote verify on the run's
    keyed pair: a forged standby-shaped checkpoint at the announced
    address — receipt-window-forked, internal-In-planted, or
    unproven — refuses no_tracking_source while the honest document
    still adopts and reconverges. The subject is the deployed pair
    while the run config keys it, else the lane-staged keyed probe
    pair."""
    case = Case(
        'demote-forged-standby-source',
        'Forged standby-source demote-verify refusal on the keyed '
        'pair',
        'with the keyed pair settled — the deployed pair, or the '
        'lane-staged probe pair while the deployed pair runs '
        'unkeyed — each pass '
        'opens the announced-only window — the tracking peer stopped, '
        'the field owner warm-restarted — so the bridge-placed forge '
        'endpoint\'s announce is the only recorded tracking hint; '
        'POST /demote then refuses no_tracking_source with no '
        'adoption or role change journaled and the peer still field '
        'owner for the tokenless endpoint serving the honest document '
        '(no valid line_proof) and for the keyed endpoint serving the '
        'receipt-window-forked and internal-In-planted forgeries '
        '(genuinely signed — the command-record audit convicts), '
        'while the same keyed endpoint serving the honest '
        'standby-shaped document verifies, journals its adoption '
        'naming the forge, and leaves the demoted peer reconverged '
        'and re-promotable; the pair restores its launch roles, and '
        'two passes produce identical digests')
    try:
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries only one endpoint — the pair '
                               'the demote-verify leg needs is absent')
        # The keyed subject (#1058): the deployed pair while the run
        # config keys it, else the lane-staged probe pair — an
        # unkeyed deployment with no staged probe pair is
        # inconclusive on capability, not on the contract.
        subject = _keyed_subject(ctx)
        if subject is None:
            return case.finish('inconclusive', 'the deployed pair '
                               'carries no --pair-token and no '
                               'keyed probe pair is staged — the '
                               'keyed announced-source contract '
                               'the leg exercises is off')
        if subject is not ctx:
            case.observe('exercised on the lane-staged keyed '
                         'probe pair — the deployed pair runs '
                         'unkeyed')
            ctx = subject
        for action in ('stop_controller', 'start_controller',
                       'restart_controller', 'start_forge',
                       'stop_forge'):
            if ctx.get(action) is None:
                return case.finish('inconclusive', 'the run context '
                                   'carries no ' + action + ' action '
                                   '— the announced-only window and '
                                   'the forged endpoint cannot be '
                                   'driven')
        placements = ctx.get('endpoint_placement') or {}
        if placements.get('forge') != 'bridge':
            return case.finish('inconclusive', 'endpoint_placement '
                               'does not place forge on the rig '
                               'bridge — a rig-dialed endpoint cannot '
                               'stand on the host')
        if not (ctx.get('journal_files') or {}).get('active') \
                or not (ctx.get('journal_files') or {}).get('standby'):
            return case.finish('inconclusive', 'the run context '
                               'carries no per-controller journal '
                               'files — the no-adoption audit cannot '
                               'run')
        for name in ('active', 'standby'):
            try:
                _role(ctx, ctx[name])
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s '
                                   'monitor is unreachable: '
                                   + str(exc)[:200])
        deadline = time.monotonic() + DEMOTE_FORGED_SETTLE
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=DEMOTE_FORGED_POLL)
        if owner is None:
            return case.finish('failed', 'no peer reports '
                               'role=active')
        peer = 'standby' if owner == 'active' else 'active'
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=DEMOTE_FORGED_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the settle the leg '
                               'restores to was never reached')
        case.observe('field owner: ' + owner + ' (' + ctx[owner]
                     + '); tracking peer: ' + peer)
        _, signals = http_json('GET', ctx[owner] + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'demote-forged-standby-signals.json',
                            signals)
        case.evidence('file', ref, 'SignalIndex naming the settled '
                      'write the forgeries contradict')
        target = _writable_bool_point(signals)
        if target is None or target.get('point') is None:
            return case.finish('inconclusive', 'the model declares '
                               'no writable bool in-point for the '
                               'forgeries to contradict')
        point = target['point']
        baseline = _point_value(_snapshot(ctx, ctx[owner]), point)
        if not isinstance(baseline, bool):
            return case.finish('inconclusive', 'point ' + str(point)
                               + ' serves no bool baseline to write '
                               'against')
        digests = []
        try:
            for number in (1, 2):
                digest, violations, evidence = \
                    _demote_forged_pass(ctx, number, owner, point,
                                        baseline)
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'demote-forged-standby-pass-' + str(number)
                    + '.json', evidence)
                case.evidence('file', ref, 'forged-demote pass '
                              + str(number) + ' — the announced '
                              'window, the staged documents, each '
                              'leg\'s demote answer with the forge '
                              'pull ledger and journal audit, the '
                              'reconvergence, and the normalized '
                              'digest')
                if violations or digest is None:
                    diagnostic = 'demote-forged-standby-failed' \
                        if digest is None or any(
                            name == 'demote-forged-standby-failed'
                            for name, _ in violations.values()) \
                        else 'demote-forged-standby-nondeterministic'
                    return case.finish(
                        'failed', diagnostic + ': ' + '; '.join(
                            detail for _, detail in
                            list(violations.values())[:4]))
                digests.append(digest)
        finally:
            # The launch layout for the cases behind this one — a
            # clean pass restores it by construction; an aborted pass
            # gets the forge removed, the peer's container back, and
            # the documented role order run again, best-effort.
            try:
                ctx['stop_forge']()
            except Exception:
                pass
            try:
                ctx['start_controller'](
                    'standby' if owner == 'active' else 'active')
            except Exception:
                pass
            current = _pair_active(ctx)
            other = 'standby' if owner == 'active' else 'active'
            if current != owner \
                    and _following_standby(ctx, owner) is not None:
                try:
                    if current is not None:
                        _settle_call(ctx[current] + '/demote')
                    _settle_call(ctx[owner] + '/promote')
                    wait_for(
                        lambda: (_pair_active(ctx) == owner or None)
                        and _tracking_standby(ctx, other),
                        time.monotonic() + DEMOTE_FORGED_SETTLE,
                        interval=DEMOTE_FORGED_POLL)
                    case.observe('cleanup: restored the entry role '
                                 'layout')
                except Exception as exc:
                    case.observe('cleanup: role restore failed: '
                                 + str(exc)[:200])
        if digests[0] != digests[1]:
            return case.finish(
                'failed', 'demote-forged-standby-nondeterministic: '
                'the two passes\' digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two forged-demote passes, identical digests')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
