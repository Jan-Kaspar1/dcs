"""The command_overflow_order acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the ordering leg extends the admission leg's contract on the
# same lane — it sits directly behind it in the settled-pair window.
RUNS_AFTER = frozenset({'scenario_command_admission'})


# --------------------------------------------------------------------
# The command-overflow submission-order contract — the lane evidence
# for #1057's fix serving WW-FND-004's receipted-command guarantee.
# Scenario 3300 proves the admission half — every submission takes a
# structured receipt, settlements land at their scan boundary — but
# not the ordering half this leg owns: under lane overflow the
# dedicated command overflow deck must hand deferred submissions back
# to the command lane in dispatch order, so the single command worker
# mints every receipt in submission order even where the flood is
# deepest, and submissions past even the overflow bound answer the
# dispatcher's named refusal — a `500` from the detached drop — rather
# than dropping silently or hanging.
#
# The mechanics mirror the unit reproduction's pinning shape: the
# paced rig's command worker drains continuously, so the lane's queue
# only fills while the worker is pinned. Submission 0 arrives with its
# body half-sent — the worker pops it and waits inside the body read
# while the labeled wave piles into the lane (LANE_QUEUE + twice the
# served command_queue.capacity deep), then onto the overflow deck
# (LANE_QUEUE deep, plus the one the feeder may hold in hand — the
# bound's edge is racy by one), and past even that bound the
# dispatcher drops the request on a detached thread whose answer is
# the named 500. Completing the stalled body starts the drain: the
# queued wave mints in order, the feeder lands each deferred
# submission at the drained tail in dispatch order, and the past-bound
# tail keeps its refusals.
#
# The audit: every admitted receipt's absolute submission index —
# correlated through the (log, high-water) pair `_receipt_window`
# serves — is monotone in submission order; the journaled
# command_settled entries follow mint order inside each terminal
# kind (a rejection journals at its mint, an admission at its
# scan-boundary settle); every past-bound submission answered the
# named refusal; and the pair keeps its launch roles throughout.
# Two consecutive passes must produce identical digests. The
# self-check leg then plants a doctored record through each audit —
# a reversed mint order and a transposed journaled pair — which must
# each report their named violation: a silent audit reports
# command-overflow-order-unchecked, the leg's own negative failing —
# it can no longer be trusted to catch what it names.

LANE_QUEUE = 64         # the monitor's declared per-lane queue bound —
                        # the command lane's spare depth and the
                        # overflow deck's bound (dcs-monitor's
                        # LANE_QUEUE_DEPTH)
OVERFLOW_MARGIN = 16    # submissions past every bound — each owed the
                        # named refusal
ORDER_MAX_CAPACITY = 512  # a served bound past this is beyond the
                          # leg's reach
PIN_LEAD = 0.5          # the stalled head's lead — the worker pops it
                        # before the wave lands
PIN_HOLD = 0.5          # the wave's pile-in window before the stalled
                        # body completes
PAIR_SETTLE = 30        # bound on the pair reporting its settled
                        # layout before the wave runs
REPLY_DEADLINE = 30     # bound on the wave's answers draining once the
                        # pin releases
SETTLE_DEADLINE = 30    # bound on the journaled settles landing
SETTLE_POLL = 0.25
DROP_STATUS = 500       # the dispatcher's named over-bound answer —
                        # the detached drop's tiny_http default


def _labeled(receipt, prefix):
    """The submission index a receipt's actor label declares, or None —
    a receipt outside the pass's labeled wave."""
    actor = (receipt or {}).get('actor')
    if not isinstance(actor, str) or not actor.startswith(prefix):
        return None
    try:
        return int(actor[len(prefix):])
    except ValueError:
        return None


def _mint_order_violation(pairs):
    """The pass's mint-order audit: `pairs` is (submission, absolute
    receipt index) in submission order — the overflow deck's hand-back
    must mint every deferred submission behind every earlier one still
    queued, so the labeled indexes climb monotonically. Returns the
    first inversion's detail, or None."""
    last = None
    for submission, index in pairs:
        if last is not None and index <= last[1]:
            return ('submission ' + str(submission) + ' minted at '
                    'receipt index ' + str(index) + ', behind '
                    'submission ' + str(last[0]) + ' at index '
                    + str(last[1]) + ' — a deferred submission minted '
                    'ahead of an earlier one')
        last = (submission, index)
    return None


def _journal_order_violation(settles):
    """The pass's journal audit: `settles` is (seq, submission, kind)
    in served order. A rejected receipt journals at its mint and an
    admitted one at its scan-boundary settle — each emission path in
    receipt-log order — so inside each terminal kind the journaled
    order is submission order. Returns the first inversion's detail,
    or None."""
    last = {}
    for seq, submission, kind in settles:
        prior = last.get(kind)
        if prior is not None and submission <= prior[0]:
            return ('the journaled ' + kind + ' for submission '
                    + str(submission) + ' at seq ' + str(seq)
                    + ' lands behind submission ' + str(prior[0])
                    + '\'s at seq ' + str(prior[1])
                    + ' — the journal fell out of mint order')
        last[kind] = (submission, seq)
    return None


def _wave_envelope(prefix, index, point):
    """The wave's `index`th command — the actor label is the receipt
    log's submission-order witness."""
    return {'command': {'write_value': {
        'point': point, 'kind': 'bool',
        'value': {'bool': index % 2 == 0}}},
        'actor': prefix + str(index)}


def _wave_request(bodies):
    """The flood channel's framing — every request sent back-to-back
    before the first answer is read; the pipelined wire the admission
    leg's `_pipelined_commands` drives."""
    request = b''
    for index, body in enumerate(bodies):
        tail = b'Connection: close\r\n' if index == len(bodies) - 1 \
            else b''
        request += (b'POST /command HTTP/1.1\r\nHost: qa\r\n'
                    b'Content-Type: application/json\r\n'
                    b'Content-Length: ' + str(len(body)).encode()
                    + b'\r\n' + tail + b'\r\n' + body)
    return request


def _read_replies(stream, count, deadline):
    """Read `count` HTTP responses off a pipelined connection —
    (status, json-body-or-None) per answer in request order; a missing
    or unframed answer reads (None, None) — the silent drop or hang
    the refusal contract forbids."""
    replies, raw = [], b''
    while len(replies) < count and time.monotonic() < deadline:
        stream.settimeout(max(0.1, deadline - time.monotonic()))
        try:
            chunk = stream.recv(65536)
        except OSError:
            break
        if not chunk:
            break
        raw += chunk
        found, raw = _parse_responses(raw)
        replies += found
    found, raw = _parse_responses(raw)
    replies += found
    return (replies + [(None, None)] * count)[:count]


def _planted_inversion(settles):
    """A doctored copy of the pass's journaled settles with one
    same-kind pair transposed — the planted negative the journal audit
    must catch — or None when every kind journaled at most once and
    there is nothing to transpose."""
    doctored = list(settles)
    for index, entry in enumerate(doctored):
        for other in range(index + 1, len(doctored)):
            if doctored[other][2] == entry[2]:
                doctored[index], doctored[other] = \
                    doctored[other], doctored[index]
                return doctored
    return None


def _overflow_pass(ctx, number, owner, peer, point, capacity):
    """One labeled wave past the lane and overflow bounds: pin the
    command worker on the half-sent head, land the wave, release the
    drain, then audit the mint order, the journaled order, the
    past-bound refusals, and the launch roles. Returns (digest,
    violations, evidence): digest is the pass's normalized verdict
    record — identical across clean passes; violations is
    {key: (diagnostic, detail)} in first-seen order."""
    base = ctx[owner]
    prefix = 'qa-overflow-order-' + str(number) + '-'
    lane_depth = LANE_QUEUE + 2 * capacity
    # The guaranteed bound: the stalled head, the lane's queued wave,
    # and the overflow deck's own bound mint for certain; one more
    # deferred submission can ride in the feeder's hand — the bound's
    # racy edge — and past it every submission must answer the named
    # refusal.
    bound = 1 + lane_depth + LANE_QUEUE
    wave = lane_depth + LANE_QUEUE + OVERFLOW_MARGIN
    violations = {}
    evidence = {'pass': number, 'owner': owner, 'capacity': capacity,
                'lane_depth': lane_depth, 'bound': bound, 'wave': wave}
    digest = {'wave': 'unseen', 'minted': 'unseen', 'deck': 'unreached',
              'past_bound': 'unreached', 'journal': 'unseen',
              'roles': 'unmoved'}

    def note(key, diagnostic, detail):
        violations.setdefault(key, (diagnostic, detail))

    def failed(key, detail):
        note(key, 'command-overflow-order-failed', detail)

    def nondet(key, detail):
        note(key, 'command-overflow-order-nondeterministic', detail)

    def finish(result):
        evidence['violations'] = {key: {'diagnostic': name,
                                        'detail': detail}
                                  for key, (name, detail)
                                  in violations.items()}
        evidence['digest'] = result
        return result, violations, evidence

    try:
        floor = _journal_cursor(ctx, base)
    except Exception as exc:
        failed('journal-floor', 'the field owner\'s journal floor '
               'never served: ' + str(exc)[:200])
        return finish(None)
    evidence['journal_floor'] = floor
    roles0 = {name: (_try_role(ctx, ctx[name]) or {}).get('role')
              for name in (owner, peer)}

    # The pin: submission 0 arrives with its declared body half-sent —
    # the reason pad pushes the Content-Length past tiny_http's eager
    # buffer so the rest stays on the socket and the one command
    # worker waits inside the body read while the wave piles in.
    stalled = json.dumps({
        'command': {'write_value': {'point': point, 'kind': 'bool',
                                    'value': {'bool': True}}},
        'actor': prefix + '0',
        'reason': 'x' * 2048}).encode()
    split = len(stalled) // 2
    try:
        stall = _connect(base)
        stall.sendall(b'POST /command HTTP/1.1\r\nHost: qa\r\n'
                      b'Content-Type: application/json\r\n'
                      b'Content-Length: ' + str(len(stalled)).encode()
                      + b'\r\n\r\n' + stalled[:split])
    except OSError as exc:
        failed('pin', 'the pinning connection never opened: '
               + str(exc)[:200])
        return finish(None)
    time.sleep(PIN_LEAD)
    bodies = [json.dumps(_wave_envelope(prefix, index, point)).encode()
              for index in range(1, wave + 1)]
    try:
        flood = _connect(base)
        flood.sendall(_wave_request(bodies))
    except OSError as exc:
        stall.close()
        failed('wave', 'the labeled wave never landed: '
               + str(exc)[:200])
        return finish(None)
    time.sleep(PIN_HOLD)
    try:
        stall.sendall(stalled[split:])
    except OSError as exc:
        flood.close()
        stall.close()
        failed('pin', 'the stalled body never completed: '
               + str(exc)[:200])
        return finish(None)
    stalled_reply = _read_replies(
        stall, 1, time.monotonic() + REPLY_DEADLINE)[0]
    stall.close()
    replies = _read_replies(flood, wave,
                            time.monotonic() + REPLY_DEADLINE)
    flood.close()
    answers = [stalled_reply] + replies
    evidence['statuses'] = [status for status, _ in answers]

    # The answer audit: no silent drop or hang anywhere; every
    # submission inside the guaranteed bound answered 200 with its own
    # receipt; the bound's racy edge answered — receipted or refused;
    # every submission past it answered the named refusal.
    unanswered = [index for index, (status, _receipt)
                  in enumerate(answers) if status is None]
    if unanswered:
        failed('silent', str(len(unanswered)) + ' submissions never '
               'answered — indexes ' + str(unanswered[:8]) + ' — the '
               'silent drop or hang the submission-order contract '
               'forbids')
    else:
        digest['wave'] = 'answered'
    minted = {}
    for index, (status, receipt) in enumerate(answers):
        if status is None:
            continue
        if index < bound:
            if status != 200:
                failed('receipt', 'submission ' + str(index)
                       + ' answered ' + str(status)
                       + ' inside the guaranteed bound — the bounded '
                       'ingress owes it a receipt')
                continue
            if _labeled(receipt, prefix) != index:
                failed('receipt', 'submission ' + str(index)
                       + ' answered with another submission\'s '
                       'receipt: ' + json.dumps(receipt)[:200])
                continue
            minted[index] = receipt
        elif index == bound:
            if status not in (200, DROP_STATUS):
                failed('edge', 'the bound-edge submission answered '
                       + str(status) + ' — receipted or the named '
                       'refusal, never this')
            elif status == 200 and _labeled(receipt, prefix) == index:
                minted[index] = receipt
        elif status != DROP_STATUS:
            failed('refusal', 'past-bound submission ' + str(index)
                   + ' answered ' + str(status) + ' — the dispatcher '
                   'owes the named ' + str(DROP_STATUS) + ' refusal')
    evidence['minted'] = len(minted)
    if len(minted) >= bound:
        digest['deck'] = 'engaged'
    elif not any(key in violations
                 for key in ('silent', 'receipt')):
        failed('deck', 'the wave minted ' + str(len(minted))
               + ' receipts against the bound ' + str(bound)
               + ' — the overflow deck never handed a deferred '
               'submission back')
    past = [index for index in range(bound + 1, wave + 1)
            if answers[index][0] == DROP_STATUS]
    if not unanswered and len(past) == wave - bound:
        digest['past_bound'] = 'named'
    elif 'refusal' not in violations and 'silent' not in violations:
        failed('refusal', 'the named ' + str(DROP_STATUS)
               + ' refusal never appeared on the past-bound tail')

    # The mint-order audit: each admitted receipt's absolute index —
    # the (log, high-water) pair correlating the bounded tail — must
    # climb in submission order.
    pairs = []
    try:
        receipts, base_index = _receipt_window(ctx, base)
        positions = {}
        for position, receipt in enumerate(receipts):
            label = _labeled(receipt, prefix)
            if label is None:
                continue
            if label in positions:
                failed('receipt-dup', 'submission ' + str(label)
                       + ' holds two receipt-log entries')
            positions[label] = base_index + position
        missing = [index for index in sorted(minted)
                   if index not in positions]
        if missing:
            failed('receipt-log', str(len(missing)) + ' minted '
                   'receipts missing from the served log — indexes '
                   + str(missing[:8]))
        extra = [index for index in positions if index not in minted]
        if extra:
            failed('receipt-extra', 'the receipt log carries '
                   'submissions the wave never answered — indexes '
                   + str(extra[:8]))
        pairs = sorted(positions.items())
        inversion = _mint_order_violation(pairs)
        if inversion is not None:
            failed('mint-order', inversion)
            digest['minted'] = 'inverted'
        elif pairs and not missing:
            digest['minted'] = 'ordered'
    except Exception as exc:
        failed('receipt-log', 'the served receipt log never read: '
               + str(exc)[:200])
    evidence['pairs'] = pairs[:8] + pairs[-4:] if len(pairs) > 12 \
        else pairs

    # The journal audit: every minted receipt owes one journaled
    # command_settled — rejections at their mint, admissions at their
    # boundary settle — the journaled order following mint order
    # inside each terminal kind.
    def journaled():
        try:
            _, journal = http_json('GET', base + '/journal?since='
                                   + str(floor))
        except Exception:
            return None
        found = []
        for entry in _journal_list(journal):
            receipt = _journal_settled(entry) or {}
            label = _labeled(receipt, prefix)
            if label is not None:
                found.append((entry.get('seq'), label,
                              _outcome_key(receipt)))
        return found
    settles = wait_for(
        lambda: (lambda found: found
                 if found is not None and len(found) >= len(minted)
                 else None)(journaled()),
        time.monotonic() + SETTLE_DEADLINE, interval=SETTLE_POLL)
    if settles is None:
        found = journaled() or []
        failed('journal', 'the journaled command_settled entries '
               'never landed — ' + str(len(found)) + ' of '
               + str(len(minted)) + ' minted receipts recorded')
        digest['journal'] = 'silent'
        settles = found
    elif len(settles) != len(minted):
        failed('journal-dup', 'the journal carries ' + str(len(settles))
               + ' labeled settles against ' + str(len(minted))
               + ' minted receipts — a settle journaled twice or a '
               'dropped submission recorded')
        digest['journal'] = 'inverted'
    else:
        inversion = _journal_order_violation(settles)
        if inversion is not None:
            failed('journal-order', inversion)
            digest['journal'] = 'inverted'
        else:
            digest['journal'] = 'ordered'
    stray = [label for _seq, label, _kind in settles
             if label not in minted]
    if stray:
        failed('journal-stray', 'journaled settles name submissions '
               'the wave never minted: ' + str(stray[:8]))
    evidence['settles'] = settles

    # The launch roles: the pin, the wave, and the drain never moved
    # either peer.
    roles1 = {name: (_try_role(ctx, ctx[name]) or {}).get('role')
              for name in (owner, peer)}
    evidence['roles'] = roles1
    if roles1 != roles0:
        nondet('roles', 'the wave moved the pair\'s launch roles: '
               + json.dumps(roles0, sort_keys=True) + ' became '
               + json.dumps(roles1, sort_keys=True))
        digest['roles'] = 'moved'
    elif None in roles1.values():
        failed('roles', 'a peer\'s /role never answered after the '
               'wave: ' + json.dumps(roles1, sort_keys=True))
        digest['roles'] = 'moved'
    else:
        digest['roles'] = 'unchanged'
    evidence['audit'] = {'pairs': pairs, 'settles': settles}
    return finish(digest)


def scenario_command_overflow_order(ctx):
    """Flood the settled field owner's command lane past its queue and
    overflow bounds with a labeled command wave — the single worker
    pinned on a half-sent head while the wave piles in — and prove the
    #1057 ordering contract: receipts mint in submission order under
    the deepest flood, the journaled command_settled entries follow
    mint order, and submissions past the overflow bound answer the
    named refusal rather than dropping or hanging."""
    case = Case('command-overflow-order',
                'Command overflow keeps receipts in submission order',
                'with the deployed pair settled, a labeled command '
                'wave past the served command_queue capacity and the '
                'lane\'s overflow bound — submitted while the single '
                'command worker is pinned on a half-sent head — '
                'mints every admitted receipt in submission order '
                '(absolute indexes monotone), journals the '
                'command_settled entries in mint order inside each '
                'terminal kind, answers every submission past the '
                'overflow bound the named refusal rather than '
                'dropping or hanging, and leaves the pair\'s launch '
                'roles standing; two consecutive passes produce '
                'identical digests and the self-check leg\'s planted '
                'negatives each report their named diagnostic')
    try:
        deadline = time.monotonic() + PAIR_SETTLE
        active = wait_for(lambda: _settled_active(ctx), deadline,
                          interval=LEG_POLL)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        if active not in ('active', 'standby'):
            return case.finish('inconclusive', 'the field owner is '
                               'the run\'s third controller — the leg '
                               'floods the launched pair')
        peer = 'standby' if active == 'active' else 'active'
        base = ctx[active]
        case.observe('labeled command flood against ' + active
                     + ' (' + base + ')')
        _, signals = http_json('GET', base + '/signals')
        target = _writable_bool_point(signals)
        if target is None or target.get('point') is None:
            return case.finish('inconclusive',
                               'no writable bool command point in '
                               'the model')
        point = target['point']
        snapshot = _snapshot(ctx, base)
        queue = snapshot.get('command_queue') or {}
        capacity = queue.get('capacity')
        ref = save_evidence(ctx['evidence_dir'],
                            'command-overflow-order-signals.json',
                            {'signals': signals,
                             'command_queue': queue})
        case.evidence('file', ref, 'signal index and the served '
                      'admission bound')
        if not isinstance(capacity, int) or isinstance(capacity, bool) \
                or capacity < 1:
            return case.finish('inconclusive',
                               'the served snapshot carries no '
                               'command_queue capacity')
        if capacity > ORDER_MAX_CAPACITY:
            return case.finish(
                'inconclusive', 'the served command_queue capacity '
                + str(capacity) + ' is beyond the leg\'s flood reach '
                '(bound ' + str(ORDER_MAX_CAPACITY) + ')')
        case.observe('served command_queue capacity ' + str(capacity)
                     + '; lane depth '
                     + str(LANE_QUEUE + 2 * capacity)
                     + ', overflow bound ' + str(LANE_QUEUE))

        digests, audits = [], []
        for number in (1, 2):
            digest, violations, evidence = _overflow_pass(
                ctx, number, active, peer, point, capacity)
            ref = save_evidence(
                ctx['evidence_dir'],
                'command-overflow-order-pass-' + str(number) + '.json',
                evidence)
            case.evidence('file', ref, 'overflow-order pass '
                          + str(number) + ' — the labeled wave\'s '
                          'answers, the mint/journal order audits, '
                          'and the normalized digest')
            if violations or digest is None:
                diagnostic = 'command-overflow-order-failed' \
                    if digest is None or any(
                        name == 'command-overflow-order-failed'
                        for name, _ in violations.values()) \
                    else 'command-overflow-order-nondeterministic'
                return case.finish(
                    'failed', diagnostic + ': ' + '; '.join(
                        detail for _, detail in
                        list(violations.values())[:4]))
            digests.append(digest)
            audits.append(evidence.get('audit') or {})
        if digests[0] != digests[1]:
            return case.finish(
                'failed', 'command-overflow-order-nondeterministic: '
                'the two passes\' digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two overflow passes, identical digests')

        # The self-check leg: each audit, run over a planted negative,
        # must name its violation — the reference plant's
        # <leg>-unchecked made leg-local: a silent audit can no longer
        # be trusted to catch what it names.
        pairs = audits[-1].get('pairs') or []
        planted = [(submission, index)
                   for (submission, _), (_, index)
                   in zip(pairs, reversed(pairs))]
        if len(planted) > 1 \
                and _mint_order_violation(planted) is None:
            return case.finish(
                'failed', 'command-overflow-order-unchecked: the '
                'mint-order audit stayed silent on a reversed record')
        settles = audits[-1].get('settles') or []
        planted = _planted_inversion(settles)
        if planted is not None \
                and _journal_order_violation(planted) is None:
            return case.finish(
                'failed', 'command-overflow-order-unchecked: the '
                'journal-order audit stayed silent on a transposed '
                'record')
        case.observe('the self-check leg\'s planted negatives each '
                     'reported their named diagnostic')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
