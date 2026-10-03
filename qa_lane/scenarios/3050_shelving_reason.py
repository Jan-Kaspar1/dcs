"""The shelving_reason acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: The shelving-reason case rides beside the alarm legs —
# self-contained on either role layout like evidence-capture and
# alarm-rationalization: whichever peer owns the field when the
# schedule reaches it serves the receipted shelve, the bounded expiry,
# and the restores it runs before returning.


# --------------------------------------------------------------------
# Decision 72's shelving-reason record on the deployed pair — the
# in-contract carriage #908 landed under the #900 decision, in
# service of WW-ALM-002's managed lifecycle: a shelve request's
# declared reason joins `actor` on the attributed POST /command
# envelope, rides the settled CommandReceipt unchanged, and echoes
# into the journaled CommandSettled entry — one durable record
# carrying actor, action, outcome, and reason beside the lifecycle
# transition the command drove. The leg resolves its target off the
# served surfaces rather than restating the model: the first managed
# alarm — kind:name order — binding a writable `shelve` input under a
# nonzero declared `max_shelve_ticks`. With the pair settled it
# submits the reasoned shelve and asserts the settled receipt's
# `reason` field, the durable journal's receipt-beside-transition
# record (the receipt's seq precedes the `shelved` point_changed, the
# pair sharing the applied tick — the pane's managed-list
# attribution), and the served surfaces the managed list joins: the
# `shelved` status port asserted live and the served journal carrying
# the same reasoned pair. The shelve then runs to its declared bound
# — the expiry transition lands at applied + max_shelve_ticks with
# the request still standing and no receipt pairing the drop, the
# automatic release attributing to no command — and the released
# request restores. The reasonless variant follows on a
# non-mandating point: a shelve without a reason settles applied and
# journals reasonless, identically to the pre-contract path. A rig
# that predates the contract — no served `requires_reason` mark, or a
# 400 refusing the reason envelope — reports inconclusive, as does a
# model declaring no shelvable managed alarm. Named diagnostics:
# shelving-reason-failed for a contract miss,
# shelving-reason-nondeterministic when the same record disagrees
# with itself across reads.

SHELVING_SETTLE = 30      # bound on the pair reporting settled
SHELVING_DEADLINE = 30    # bound on each settle/observe wait
SHELVING_EXPIRY = 60      # bound on the declared expiry landing
SHELVING_LIVE_POLL = 0.1  # cadence catching the bounded shelved window
SHELVING_ACTOR = 'qa-lane'
SHELVING_REASON = 'qa-lane shelving-reason audit'
SHELVING_RELEASE_REASON = 'qa-lane shelving-reason restore'

# The managed kinds — either sibling carries the shelve contract.
SHELVING_KINDS = ('managed-latching-alarm',
                  'managed-bool-latching-alarm')


def _shelve_write(point, level):
    """The receipted-path shelve request — a bool `write_value` on the
    managed alarm's `shelve` input point, level-observed."""
    return {'write_value': {'point': point, 'kind': 'bool',
                            'value': {'bool': level}}}


def _shelve_binding(descriptor):
    """(shelve, shelved) — the request input and the managed-list
    status output a managed descriptor binds — or None."""
    shelve = shelved = None
    for port in descriptor.get('ports') or []:
        if not isinstance(port, dict):
            continue
        if port.get('name') == 'shelve' \
                and port.get('direction') == 'in':
            shelve = port.get('point')
        elif port.get('name') == 'shelved' \
                and port.get('direction') == 'out':
            shelved = port.get('point')
    if shelve is None or shelved is None:
        return None
    return shelve, shelved


def _shelve_target(signals, snap):
    """(target, problem) — the leg's shelve target resolved off the
    served index and descriptors: the first managed alarm, in
    kind:name order, binding a writable `shelve` point and a `shelved`
    status point under a nonzero declared `max_shelve_ticks`; the
    target also carries `reasonless`, the first such candidate whose
    point declaration does not mark `requires_reason`. problem is
    (outcome, detail) — inconclusive when the rig predates the
    contract (no served `requires_reason` mark) or declares no
    shelvable managed alarm."""
    points = {entry.get('point'): entry
              for entry in signals.get('points') or []
              if isinstance(entry, dict)}
    descriptors = [entry for entry in snap.get('descriptors') or []
                   if isinstance(entry, dict)
                   and entry.get('kind') in SHELVING_KINDS]
    if not descriptors:
        return None, ('inconclusive', 'the served descriptors carry '
                      'no managed alarm instance — the deployed model '
                      'is not the rig\'s managed set')
    if not any('requires_reason' in entry for entry in points.values()):
        return None, ('inconclusive', 'no served point carries the '
                      'requires_reason mark — the deployed build '
                      'predates the shelving-reason contract')
    candidates = []
    for descriptor in sorted(descriptors,
                             key=lambda entry: str(entry.get('name'))):
        binding = _shelve_binding(descriptor)
        if binding is None:
            continue
        shelve, shelved = binding
        meta = points.get(shelve)
        if meta is None or not meta.get('writable'):
            continue
        bound = _parameter_value(snap, descriptor.get('name'),
                                 'max_shelve_ticks')
        if not isinstance(bound, int) or isinstance(bound, bool) \
                or bound <= 0:
            continue
        candidates.append({'component': descriptor.get('name'),
                           'shelve': shelve, 'shelved': shelved,
                           'bound': bound,
                           'requires_reason':
                               bool(meta.get('requires_reason'))})
    if not candidates:
        return None, ('inconclusive', 'no managed alarm binds a '
                      'writable shelve input under a declared '
                      'max_shelve_ticks — the deployed model offers '
                      'the leg nothing to shelve')
    target = dict(candidates[0])
    target['reasonless'] = next(
        (entry for entry in candidates
         if not entry['requires_reason']), None)
    if target['reasonless'] is None:
        return None, ('inconclusive', 'every shelvable managed alarm '
                      'declares requires_reason — the rig offers no '
                      'non-mandating point for the reasonless leg')
    return target, None


def _settle_and_flag(ctx, base, index, command, shelved_point,
                     deadline):
    """Watch the submitted write's terminal receipt and the served
    `shelved` assertion in one poll loop — the bounded shelve window
    closes `max_shelve_ticks` scans after the applied tick, so the
    managed-list membership must be caught while the settle lands.
    Returns (settled, observed) — observed carries the snapshot tick
    and sample of the first served assertion, or None."""
    settled = observed = None
    while time.monotonic() < deadline:
        if settled is None:
            got = _submitted_receipt(ctx, base, index, command)
            if got is not None:
                settled = got
        if observed is None:
            snap = _try_snapshot(ctx, base)
            if snap is not None \
                    and _point_value(snap, shelved_point) is True:
                observed = {'tick': snap.get('tick'),
                            'sample': _point_sample(snap,
                                                    shelved_point)}
        if settled is not None and observed is not None:
            break
        time.sleep(SHELVING_LIVE_POLL)
    return settled, observed


def _shelve_round(ctx, base, command, reason, deadline, watch=None):
    """One receipted shelve write: POST the attributed envelope — the
    leg's actor always, `reason` only when the submission declares
    one — then wait the terminal receipt at the captured submission
    index. `watch`, when given, is the wait — the reasoned leg's
    combined settle-and-flag poll. Returns the leg's audit record."""
    envelope = {'command': command, 'actor': SHELVING_ACTOR}
    if reason is not None:
        envelope['reason'] = reason
    record = {'command': command, 'envelope': envelope}
    index = _next_receipt_index(ctx, base)
    try:
        status, receipt = http_json('POST', base + '/command', envelope)
    except urllib.error.HTTPError as exc:
        record['status'] = exc.code
        try:
            record['receipt'] = json.loads(exc.read() or b'null')
        except Exception:
            record['receipt'] = None
        finally:
            exc.close()
        return record
    record['status'] = status
    record['receipt'] = receipt
    record['index'] = index
    if status != 200:
        return record
    if watch is not None:
        settled, record['observed'] = watch(index)
        record['settled'] = settled
    else:
        record['settled'] = wait_for(
            lambda: _submitted_receipt(ctx, base, index, command),
            deadline, interval=SHELVING_LIVE_POLL)
    return record


def _shelve_lifecycle(records, shelve_point, shelved_point):
    """The leg's lifecycle records out of parsed journal entries —
    `{'seq','tick','receipt'}` for each command_settled on the shelve
    request point and `{'seq','tick','from','to'}` for each
    point_changed on the shelved status point, in seq order. Reads
    both wire shapes: `--journal-file` records and served
    /journal entries."""
    receipts, transitions = [], []
    for item in records:
        body = item.get('entry') if isinstance(item.get('entry'), dict) \
            else item
        if not isinstance(body, dict):
            continue
        receipt = _journal_settled(item)
        if receipt is not None:
            write = (receipt.get('command') or {}).get('write_value') \
                or {}
            if write.get('point') == shelve_point:
                receipts.append({'seq': body.get('seq'),
                                 'tick': body.get('tick'),
                                 'receipt': receipt})
            continue
        observed = _journal_observation(item)
        if observed is not None and observed[0] == 'point_changed' \
                and observed[1] == shelved_point:
            transitions.append({'seq': body.get('seq'),
                                'tick': body.get('tick'),
                                'from': observed[2], 'to': observed[3]})
    return receipts, transitions


def _file_lifecycle(path, shelve_point, shelved_point):
    """`_shelve_lifecycle` over the field owner's --journal-file."""
    return _shelve_lifecycle(_journal_entries(path), shelve_point,
                             shelved_point)


def _lifecycle_ready(records, want_drop=False):
    """(receipts, transitions, rise, drop) once a parsed lifecycle
    carries a settled request receipt and the `shelved` rise — plus
    the expiry drop after the rise when `want_drop` — else None."""
    receipts, transitions = records
    rise = next((entry for entry in transitions
                 if entry.get('to') == {'bool': True}), None)
    if not receipts or rise is None:
        return None
    drop = next((entry for entry in transitions
                 if entry.get('to') == {'bool': False}
                 and (rise.get('seq') is None
                      or entry.get('seq') is None
                      or entry['seq'] > rise['seq'])), None)
    if want_drop and drop is None:
        return None
    return receipts, transitions, rise, drop


def _lifecycle_wait(ctx, base, journal_path, shelve_point,
                    shelved_point, deadline, want_drop=False):
    """Poll the field owner's --journal-file until the shelve's
    lifecycle is durably recorded. Each poll also reads the served
    journal — the monitor's own surface — so the wait rides the rig's
    cadence rather than the file's flush alone; a dropped read is one
    lost poll, never the leg's verdict."""
    result = []

    def check():
        try:
            http_json('GET', base + '/journal?since=0')
        except Exception:
            pass
        ready = _lifecycle_ready(
            _file_lifecycle(journal_path, shelve_point, shelved_point),
            want_drop)
        if ready is not None:
            result.append(ready)
            return True
        return False

    wait_for(check, deadline, interval=SHELVING_LIVE_POLL)
    return result[-1] if result else None


def scenario_shelving_reason(ctx):
    """The deployed pair's shelving-reason carriage: a shelve
    submitted with a declared reason settles applied through the
    receipted path with the reason on the settled receipt, journaled
    beside the `shelved` lifecycle transition, and exposed on the
    served managed-state surfaces; the shelve expires at the declared
    bound with the request still standing; a reasonless shelve on a
    non-mandating point settles identically to the pre-contract
    path."""
    case = Case(
        'shelving-reason',
        'Receipted shelving-reason carriage and bounded expiry',
        'with the deployed pair settled, a shelve submitted with a '
        'declared reason on the rig\'s shelvable managed alarm '
        'settles applied through the receipted path — the settled '
        'receipt carrying the declared actor and reason — the '
        'durable journal carries the attributed receipt beside the '
        'shelved point_changed transition at the applied tick, the '
        'served journal exposes the same pair the managed list '
        'renders while the snapshot reports shelved asserted, the '
        'expiry drop lands at applied + max_shelve_ticks with the '
        'request still standing and no receipt pairing it, the '
        'released request restores, a reasonless shelve on a '
        'non-mandating point settles applied and journals without a '
        'reason, and a re-read serves the same record')
    held = None   # (base, point) while a shelve request may stand
    try:
        # Self-contained on either role layout — the shelve rides
        # whichever peer owns the field when the schedule reaches the
        # leg, and the restores run before returning.
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + SHELVING_SETTLE)
        if active is None:
            reachable = any(
                _try_role(ctx, ctx[name]) is not None
                for name in ('active', 'standby') if ctx.get(name))
            return case.finish(
                'failed' if reachable else 'inconclusive',
                'no peer reports role=active' if reachable
                else 'the pair is unreachable')
        base = ctx[active]
        journal_path = (ctx.get('journal_files') or {}).get(active)
        if journal_path is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no journal-file path for the '
                               'field owner')
        case.observe('shelving-reason audit against ' + active
                     + ' (' + base + ')')

        _, signals = http_json('GET', base + '/signals')
        snap = _snapshot(ctx, base)
        target, problem = _shelve_target(signals, snap)
        ref = save_evidence(ctx['evidence_dir'],
                            'shelving-reason-surfaces.json',
                            {'target': target,
                             'points': signals.get('points'),
                             'descriptors': snap.get('descriptors')})
        case.evidence('file', ref, 'the served index and descriptors '
                      'the shelve target resolved from')
        if problem is not None:
            return case.finish(problem[0], problem[1])
        component = target['component']
        shelve_point = target['shelve']
        shelved_point = target['shelved']
        bound = target['bound']
        case.observe('shelve target ' + str(component)
                     + ': request point ' + str(shelve_point)
                     + ' -> shelved point ' + str(shelved_point)
                     + ', bound ' + str(bound) + ' ticks')

        cursor = _journal_cursor(ctx, base)

        # The reasoned leg: the shelve request carries the declared
        # reason on the attributed envelope; the settle wait doubles
        # as the managed-list watch so the bounded `shelved` window is
        # caught while it stands.
        command = _shelve_write(shelve_point, True)
        deadline = time.monotonic() + SHELVING_DEADLINE
        submit = _shelve_round(
            ctx, base, command, SHELVING_REASON, deadline,
            watch=lambda index: _settle_and_flag(
                ctx, base, index, command, shelved_point, deadline))
        if submit.get('status') == 400:
            ref = save_evidence(ctx['evidence_dir'],
                                'shelving-reason-settle.json', submit)
            case.evidence('file', ref, 'the refused reasoned '
                          'submission')
            return case.finish('inconclusive', 'the attributed '
                               'reason envelope was refused — the '
                               'deployed build predates the '
                               'shelving-reason contract')
        if submit.get('status') == 200:
            held = (base, shelve_point)
        settled = submit.get('settled')
        observed = submit.get('observed')
        ref = save_evidence(ctx['evidence_dir'],
                            'shelving-reason-settle.json', submit)
        case.evidence('file', ref, 'the reasoned submission, its '
                      'settled receipt, and the served shelved '
                      'assertion')
        if submit.get('status') != 200:
            return case.finish('failed', 'shelving-reason-failed: '
                               'the reasoned shelve submission '
                               'answered HTTP '
                               + str(submit.get('status')) + ': '
                               + json.dumps(submit.get('receipt'),
                                            sort_keys=True)[:300])
        if settled is None:
            return case.finish('failed', 'shelving-reason-failed: '
                               'the reasoned shelve never settled '
                               'a terminal receipt')
        if _outcome_key(settled) != 'applied':
            return case.finish('failed', 'shelving-reason-failed: '
                               'the reasoned shelve settled '
                               + _outcome_key(settled) + ': '
                               + json.dumps(settled,
                                            sort_keys=True)[:300])
        bad = []
        if settled.get('actor') != SHELVING_ACTOR:
            bad.append('the settled receipt lost the declared '
                       'actor: ' + json.dumps(settled.get('actor')))
        if settled.get('reason') != SHELVING_REASON:
            bad.append('the settled receipt lost the declared '
                       'reason: ' + json.dumps(settled.get('reason')))
        if bad:
            return case.finish('failed', 'shelving-reason-failed: '
                               + '; '.join(bad))
        applied = (settled.get('outcome') or {}).get('applied') or {}
        applied_tick = applied.get('tick')
        if not isinstance(applied_tick, int):
            return case.finish('failed', 'shelving-reason-failed: '
                               'the applied outcome carries no tick: '
                               + json.dumps(settled,
                                            sort_keys=True)[:300])
        if observed is None:
            return case.finish('failed', 'shelving-reason-failed: '
                               'the served snapshot never reported '
                               'shelved asserted — the managed list '
                               'never routed ' + str(component))
        case.observe('shelve settled applied at tick '
                     + str(applied_tick)
                     + ' carrying the declared reason; shelved '
                     'served asserted at tick '
                     + str(observed.get('tick')))

        # The durable record: the field owner's journal carries the
        # attributed receipt beside the lifecycle transition — the
        # receipt's seq precedes the shelved rise and the pair shares
        # the applied tick, the attribution the managed list renders.
        lifecycle = _lifecycle_wait(ctx, base, journal_path,
                                  shelve_point, shelved_point,
                                  time.monotonic() + SHELVING_DEADLINE)
        if lifecycle is None:
            return case.finish('failed', 'shelving-reason-failed: '
                               'the durable journal never recorded '
                               'the shelve — no settled receipt on '
                               'the request point or no shelved '
                               'transition')
        receipts, transitions, rise, _drop = lifecycle
        reasoned = [entry for entry in receipts
                    if (entry['receipt'].get('command') == command
                        and 'applied' in
                        (entry['receipt'].get('outcome') or {}))]
        bad = []
        if not reasoned:
            bad.append('no journaled applied receipt names the '
                       'shelve write')
        else:
            journaled = reasoned[-1]['receipt']
            if journaled.get('reason') != SHELVING_REASON:
                bad.append('the journaled receipt lost the declared '
                           'reason: '
                           + json.dumps(journaled.get('reason')))
            if journaled.get('actor') != SHELVING_ACTOR:
                bad.append('the journaled receipt lost the declared '
                           'actor: '
                           + json.dumps(journaled.get('actor')))
            if reasoned[-1].get('seq') is not None \
                    and rise.get('seq') is not None \
                    and not reasoned[-1]['seq'] < rise['seq']:
                bad.append('the journaled receipt (seq '
                           + str(reasoned[-1].get('seq'))
                           + ') does not precede the shelved '
                           'transition (seq ' + str(rise.get('seq'))
                           + ')')
        if rise.get('tick') != applied_tick:
            bad.append('the shelved rise journaled at tick '
                       + str(rise.get('tick')) + ' — the applied '
                       'receipt settled at ' + str(applied_tick)
                       + ', so the transition pairs no reasoned '
                       'receipt')
        if bad:
            return case.finish('failed', 'shelving-reason-failed: '
                               + '; '.join(bad))
        case.observe('the durable journal carries the attributed '
                     'receipt (seq ' + str(reasoned[-1].get('seq'))
                     + ') beside the shelved rise (seq '
                     + str(rise.get('seq')) + ', tick '
                     + str(rise.get('tick')) + ')')

        # The served surface: the journal the monitor serves carries
        # the same reasoned pair — the managed-list surface's record —
        # beside the live flag already reported.
        _, served = http_json('GET', base + '/journal?since='
                              + str(cursor))
        served_receipts, served_transitions = _shelve_lifecycle(
            _journal_list(served), shelve_point, shelved_point)
        served_reasoned = [entry for entry in served_receipts
                           if entry['receipt'].get('command')
                           == command]
        served_rise = next(
            (entry for entry in served_transitions
             if entry.get('to') == {'bool': True}), None)
        bad = []
        if not served_reasoned \
                or served_reasoned[-1]['receipt'].get('reason') \
                != SHELVING_REASON:
            bad.append('the served journal carries no settled '
                       'receipt on the request point bearing the '
                       'declared reason')
        if served_rise is None:
            bad.append('the served journal carries no shelved '
                       'transition the reasoned receipt pairs')
        if bad:
            return case.finish('failed', 'shelving-reason-failed: '
                               + '; '.join(bad))
        case.observe('the served journal exposes the reasoned '
                     'receipt beside the shelved transition — the '
                     'managed-list record')

        # The expiry leg: the shelve runs to its declared bound —
        # the drop lands at applied + max_shelve_ticks with the
        # request still standing, attributing to no command.
        lifecycle = _lifecycle_wait(ctx, base, journal_path,
                                  shelve_point, shelved_point,
                                  time.monotonic() + SHELVING_EXPIRY,
                                  want_drop=True)
        ref = save_evidence(
            ctx['evidence_dir'],
            'shelving-reason-expiry.json',
            {'applied_tick': applied_tick, 'bound': bound,
             'lifecycle': {
                 'receipts': lifecycle[0],
                 'transitions': lifecycle[1]} if lifecycle else None})
        case.evidence('file', ref, 'the durable lifecycle through '
                      'the declared expiry bound')
        if lifecycle is None:
            return case.finish('failed', 'shelving-reason-failed: '
                               'the shelved flag never expired — '
                               'the durable journal shows no drop '
                               'past the declared bound '
                               + str(bound))
        _receipts, transitions, rise, drop = lifecycle
        bad = []
        if drop.get('tick') != applied_tick + bound:
            bad.append('the expiry drop journaled at tick '
                       + str(drop.get('tick')) + ' — the declared '
                       'bound lands it at ' + str(applied_tick + bound))
        paired = [entry for entry in _file_lifecycle(
            journal_path, shelve_point, shelved_point)[0]
                  if ((entry['receipt'].get('outcome') or {})
                      .get('applied') or {}).get('tick')
                  == drop.get('tick')]
        if paired:
            bad.append('a settled receipt pairs the expiry tick '
                       + str(drop.get('tick'))
                       + ' — the automatic release must attribute '
                       'to no command: '
                       + json.dumps(paired[0]['receipt'],
                                    sort_keys=True)[:200])
        snap = _try_snapshot(ctx, base)
        standing = _point_value(snap or {}, shelve_point)
        if standing is not True:
            bad.append('the shelve request read '
                       + json.dumps(standing) + ' at the expiry — '
                       'the bound\'s release must land with the '
                       'request still standing')
        if _point_value(snap or {}, shelved_point) is not False:
            bad.append('the served snapshot still reports shelved '
                       'after the declared bound')
        if bad:
            return case.finish('failed', 'shelving-reason-failed: '
                               + '; '.join(bad))
        case.observe('the shelve expired at tick '
                     + str(drop.get('tick')) + ' — applied '
                     + str(applied_tick) + ' + bound ' + str(bound)
                     + ' — with the request standing and no '
                     'command pairing the drop')

        # The restore: the held request releases through the same
        # receipted path — the release carries its own declared
        # reason — leaving the request point false.
        release = _shelve_round(ctx, base,
                                _shelve_write(shelve_point, False),
                                SHELVING_RELEASE_REASON,
                                time.monotonic() + SHELVING_DEADLINE)
        ref = save_evidence(ctx['evidence_dir'],
                            'shelving-reason-release.json', release)
        case.evidence('file', ref, 'the release submission and its '
                      'settled receipt')
        if release.get('status') != 200 \
                or _outcome_key(release.get('settled')) != 'applied':
            return case.finish('failed', 'shelving-reason-failed: '
                               'the release never settled applied: '
                               + json.dumps(release,
                                            sort_keys=True)[:300])
        held = None

        # The reasonless variant: a shelve without a reason on the
        # non-mandating declaration settles identically to the
        # pre-contract path — applied, journaled, reasonless.
        plain = target['reasonless']
        pcommand = _shelve_write(plain['shelve'], True)
        psubmit = _shelve_round(ctx, base, pcommand, None,
                                time.monotonic() + SHELVING_DEADLINE)
        if psubmit.get('status') == 200:
            held = (base, plain['shelve'])
        ref = save_evidence(ctx['evidence_dir'],
                            'shelving-reason-reasonless.json',
                            psubmit)
        case.evidence('file', ref, 'the reasonless submission and '
                      'its settled receipt')
        if psubmit.get('status') != 200:
            return case.finish('failed', 'shelving-reason-failed: '
                               'the reasonless shelve submission '
                               'answered HTTP '
                               + str(psubmit.get('status')))
        psettled = psubmit.get('settled')
        if psettled is None:
            return case.finish('failed', 'shelving-reason-failed: '
                               'the reasonless shelve never settled '
                               'a terminal receipt')
        if _outcome_key(psettled) != 'applied':
            return case.finish('failed', 'shelving-reason-failed: '
                               'the reasonless shelve on the '
                               'non-mandating point settled '
                               + _outcome_key(psettled)
                               + ' — the declaration mandates no '
                               'reason, so the path is the '
                               'pre-contract one')
        if psettled.get('reason') is not None:
            return case.finish('failed', 'shelving-reason-failed: '
                               'the reasonless shelve\'s receipt '
                               'carries a reason the submission '
                               'never declared: '
                               + json.dumps(psettled.get('reason')))

        # Its lifecycle journals identically minus the field: a
        # reasonless settled receipt beside a fresh shelved rise.
        lifecycle = _lifecycle_wait(ctx, base, journal_path,
                                  plain['shelve'], plain['shelved'],
                                  time.monotonic() + SHELVING_DEADLINE)
        bad = []
        if lifecycle is None:
            bad.append('the durable journal never recorded the '
                       'reasonless shelve\'s lifecycle')
        else:
            preceipts, _pt, _prise, _pdrop = lifecycle
            plain_settled = [entry for entry in preceipts
                             if entry['receipt'].get('command')
                             == pcommand
                             and 'applied' in
                             (entry['receipt'].get('outcome') or {})]
            if not plain_settled:
                bad.append('no journaled applied receipt names the '
                           'reasonless shelve write')
            elif plain_settled[-1]['receipt'].get('reason') \
                    is not None:
                bad.append('the journaled reasonless receipt '
                           'carries a reason: '
                           + json.dumps(plain_settled[-1]['receipt']
                                        .get('reason')))
        if bad:
            return case.finish('failed', 'shelving-reason-failed: '
                               + '; '.join(bad))
        case.observe('the reasonless shelve settled applied and '
                     'journaled without a reason — identical to the '
                     'pre-contract path')

        # Restore the reasonless request the same way — the rig
        # returns with no shelve request standing.
        prelease = _shelve_round(ctx, base,
                                 _shelve_write(plain['shelve'], False),
                                 None,
                                 time.monotonic() + SHELVING_DEADLINE)
        if prelease.get('status') != 200 \
                or _outcome_key(prelease.get('settled')) != 'applied':
            return case.finish('failed', 'shelving-reason-failed: '
                               'the reasonless release never settled '
                               'applied: '
                               + json.dumps(prelease,
                                            sort_keys=True)[:300])
        held = None

        # The consistency leg: the durable record re-read must serve
        # the same receipts and transitions — a record disagreeing
        # with itself is the named nondeterministic miss.
        receipts2, transitions2 = _file_lifecycle(
            journal_path, shelve_point, shelved_point)
        ref = save_evidence(
            ctx['evidence_dir'],
            'shelving-reason-reread.json',
            {'first': {'receipts': receipts,
                       'transitions': transitions},
             'second': {'receipts': receipts2,
                        'transitions': transitions2}})
        case.evidence('file', ref, 'the lifecycle record re-read '
                      'after the restores')
        if receipts2[:len(receipts)] != receipts \
                or transitions2[:len(transitions)] != transitions:
            return case.finish(
                'failed', 'shelving-reason-nondeterministic: the '
                'journaled record moved between reads')

        if _settled_active(ctx) != active:
            return case.finish('failed', 'shelving-reason-failed: '
                               'the pair\'s roles moved during the '
                               'leg')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        # A shelve request left standing poisons later legs — the
        # release is best-effort on whichever peer owns the field.
        if held is not None:
            hbase, hpoint = held
            try:
                http_json('POST', hbase + '/command',
                          {'command': _shelve_write(hpoint, False),
                           'actor': SHELVING_ACTOR})
            except Exception:
                pass
