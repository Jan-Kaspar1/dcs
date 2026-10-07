"""The command_abort_verdict acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the abort-verdict leg rides the same command-lane pin the
# ordering leg's labeled wave proves — it sits directly behind it in
# the settled-pair window.
RUNS_AFTER = frozenset({'scenario_command_overflow_order'})


# --------------------------------------------------------------------
# The aborted command transport contract: ending a client wait does not
# cancel a command already sent to the controller. Stall the command lane,
# submit a labeled write under a short read bound, and then drain the lane.
# The served receipts, both served journals, durable journal files, images,
# and launch roles must agree on exactly one terminal settlement.

DIAG_FAILED = 'command-abort-verdict-failed'
DIAG_NONDET = 'command-abort-verdict-nondeterministic'
DIAG_UNCHECKED = 'command-abort-verdict-unchecked'

ABORT_LEAD = 0.6       # the pin's lead — the command worker pops the
                       # stalled head before the abandoned post lands
ABORT_HOLD = 0.8       # the pinned window the abort rides inside —
                       # longer than the post's tightened bound
ABORT_BOUND = 0.3      # client read bound for the abandoned submission
ABORT_SETTLE = 30      # bound on the pair settling, the journaled
                       # settles landing, and each best-effort restore
ABORT_REPLY = 15       # bound on the pin's answer draining after the
                       # stalled body completes
ABORT_POLL = 0.25      # wait cadence inside the leg
STALLED_PAD = 2048     # the reason pad pushing the pinning body's
                       # Content-Length past tiny_http's eager buffer


def _drain_reply(stream, deadline):
    """One HTTP response off a connection — (status, json-body-or-None)
    — (None, None) when the answer never completes: the released pin's
    drain answer."""
    replies, raw = [], b''
    while not replies and time.monotonic() < deadline:
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
    return replies[0] if replies else (None, None)


def _post_command(base, body, bound):
    """Send one POST /command under a bounded socket wait.

    Return an answered receipt, an answered endpoint refusal, or an
    unanswered transport result. An unanswered result ends the client
    wait without determining whether the controller will apply the command.
    """
    payload = json.dumps(body).encode()
    request = (b'POST /command HTTP/1.1\r\nHost: qa\r\n'
               b'Content-Type: text/plain\r\nContent-Length: '
               + str(len(payload)).encode()
               + b'\r\nConnection: close\r\n\r\n' + payload)
    try:
        stream = _connect(base, timeout=bound)
    except OSError as exc:
        return 'unanswered', str(exc)[:200]
    try:
        try:
            stream.sendall(request)
        except OSError as exc:
            return 'unanswered', str(exc)[:200]
        replies, raw = [], b''
        deadline = time.monotonic() + bound
        while not replies and time.monotonic() < deadline:
            stream.settimeout(max(0.01, deadline - time.monotonic()))
            try:
                chunk = stream.recv(65536)
            except OSError as exc:
                return 'unanswered', str(exc)[:200]
            if not chunk:
                break
            raw += chunk
            found, raw = _parse_responses(raw)
            replies += found
        found, raw = _parse_responses(raw)
        replies += found
        if not replies:
            return 'unanswered', 'the read bound fired unanswered'
        status, parsed = replies[0]
        if status is None:
            return 'unanswered', 'the answer never framed'
        if 400 <= status < 500:
            return 'refused', (status, parsed)
        if 200 <= status < 300:
            return ('answered', parsed) if isinstance(parsed, dict) \
                else ('unanswered', 'the receipt never parsed')
        return 'unanswered', 'the monitor answered HTTP ' + str(status)
    finally:
        stream.close()


def _judge_settle(record, note):
    """The settled-truth audit: the admission's one terminal outcome —
    journaled once per peer, carried identically in each served
    receipt log and durable journal file, and applied to the served
    image at most once."""
    audit = record.get('audit') or {}
    owner, peer = record['owner'], record['peer']
    want = (record.get('admission') or {}).get('value')
    journaled = audit.get('journaled') or {}
    outcomes = {key for entries in journaled.values()
                for key in entries}
    outcome = next(iter(outcomes)) if len(outcomes) == 1 else None
    if not outcomes:
        note('settled', DIAG_FAILED,
             'the abandoned submission never reached a terminal '
             'journaled verdict — the receipted path owes every '
             'admitted command exactly one settle')
    elif len(outcomes) != 1:
        note('outcomes', DIAG_NONDET,
             'the abandoned admission journaled '
             + json.dumps(sorted(outcomes)) + ' — one admission, '
             'never more than one terminal outcome')
    elif outcome != 'applied' and not outcome.startswith('rejected:'):
        note('outcome', DIAG_NONDET,
             'the abandoned admission settled ' + outcome
             + ' — outside the terminal vocabulary: applied or a '
             'named refusal')
    for name in (owner, peer):
        entries = journaled.get(name)
        if entries is None or len(entries) != 1:
            note('journal-' + name, DIAG_NONDET,
                 name + ' journaled ' + str(len(entries or []))
                 + ' command_settled records for the abandoned '
                 'admission — exactly one stands per peer')
        if outcome is not None:
            logged = (audit.get('logged') or {}).get(name)
            if logged != [outcome]:
                note('log-' + name, DIAG_NONDET,
                     name + '\'s adopted receipt log carries '
                     + json.dumps(logged) + ' for the abandoned '
                     'admission where the journal settled ' + outcome)
        files = (audit.get('files') or {}).get(name)
        if files is None or not files:
            note('file-' + name, DIAG_FAILED,
                 name + '\'s durable journal carries no readable '
                 'settle for the abandoned admission — the durable '
                 'journal owes the command\'s true terminal verdict')
        elif len(files) != 1:
            note('file-' + name, DIAG_NONDET,
                 name + '\'s durable journal carries ' + str(len(files))
                 + ' settle records for the abandoned admission — '
                 'exactly one stands per peer')
        elif outcome is not None and files[0] != outcome:
            note('file-' + name + '-outcome', DIAG_NONDET,
                 name + '\'s durable journal carries ' + files[0]
                 + ' where the served journal settled ' + outcome)
        if outcome is not None:
            image = (audit.get('image') or {}).get(name)
            if outcome == 'applied' and image != want:
                note('image-' + name, DIAG_NONDET,
                     name + '\'s served image never took the abandoned '
                     'admission\'s applied write — serves '
                     + json.dumps(image))
            if outcome != 'applied' and image == want:
                note('image-phantom-' + name, DIAG_NONDET,
                     name + '\'s served image carries the refused '
                     'admission\'s value — an application the journal '
                     'never settled')


def _judge_roles(record, note):
    """The launch-role audit: the pin, the abort, and the drain never
    move the pair off its launch roles."""
    roles = record.get('roles') or {}
    launch = record.get('launch_roles') or {}
    if roles != launch:
        note('roles', DIAG_NONDET,
             'the aborted-post pass moved the pair\'s launch roles: '
             + json.dumps(launch, sort_keys=True) + ' became '
             + json.dumps(roles, sort_keys=True))
    elif None in roles.values():
        note('roles-answer', DIAG_FAILED,
             'a peer\'s /role never answered after the pass: '
             + json.dumps(roles, sort_keys=True))


def _self_check():
    """Require the settlement and role audits to reject planted faults."""
    slipped = []

    def clean():
        command = {'write_value': {'point': 204, 'kind': 'bool',
                                   'value': {'bool': True}}}
        return {'owner': 'active', 'peer': 'standby',
                'launch_roles': {'active': 'active',
                                 'standby': 'standby'},
                'admission': {'command': command,
                              'actor': 'qa-abort-verdict-1',
                              'value': True},
                'audit': {'outcome': 'applied',
                          'journaled': {'active': ['applied'],
                                        'standby': ['applied']},
                          'logged': {'active': ['applied'],
                                     'standby': ['applied']},
                          'files': {'active': ['applied'],
                                    'standby': ['applied']},
                          'image': {'active': True, 'standby': True}},
                'roles': {'active': 'active', 'standby': 'standby'}}

    def every(record):
        def judge(note):
            _judge_settle(record, note)
            _judge_roles(record, note)
        return judge

    def expect(name, judge):
        found = []
        judge(lambda key, diagnostic, detail: found.append(key))
        if not found:
            slipped.append(name)

    # The abandoned submission that never reached a terminal verdict.
    record = clean()
    record['audit'] = {'outcome': None,
                       'journaled': {'active': [], 'standby': []},
                       'logged': {'active': [], 'standby': []},
                       'files': {'active': [], 'standby': []},
                       'image': {'active': False, 'standby': False}}
    expect('never-settled', every(record))
    # One admission settling twice on a peer.
    record = clean()
    record['audit']['journaled']['standby'] = ['applied', 'applied']
    expect('double-settle', every(record))
    # The tracking peer contradicting the settled verdict.
    record = clean()
    record['audit']['journaled']['standby'] = ['rejected:queue_full']
    expect('contradictory-outcome', every(record))
    # An adopted receipt log still pending where the journal settled.
    record = clean()
    record['audit']['logged']['standby'] = ['accepted']
    expect('pending-log', every(record))
    # The durable record missing the settle the served journal shows.
    record = clean()
    record['audit']['files']['active'] = []
    expect('missing-file-settle', every(record))
    # The applied write the served image never took.
    record = clean()
    record['audit']['image']['standby'] = False
    expect('phantom-image', every(record))
    # A role moved under the pass.
    record = clean()
    record['roles'] = {'active': 'active', 'standby': 'promoting'}
    expect('moved-role', every(record))
    return slipped


def _restore_launch_roles(ctx, launch):
    """The exit the rig is owed: walk the pair back to its launch
    roles — a clean pass moves none, so this is the belt against an
    aborted pass's mid-flight residue."""
    owner = next((name for name, role in launch.items()
                  if role == 'active'), None)
    peer = next((name for name, role in launch.items()
                 if role == 'standby'), None)
    if owner is None or peer is None:
        return
    try:
        report = _try_role(ctx, ctx[peer])
        if (report or {}).get('role') in ('active', 'promoting'):
            _settle_call(ctx[peer] + '/demote')
            wait_for(lambda: (_try_role(ctx, ctx[peer]) or {})
                     .get('role') == 'standby' or None,
                     time.monotonic() + ABORT_SETTLE,
                     interval=ABORT_POLL)
        if (_try_role(ctx, ctx[owner]) or {}).get('role') != 'active':
            _settle_call(ctx[owner] + '/promote')
            wait_for(lambda: (_try_role(ctx, ctx[owner]) or {})
                     .get('role') == 'active' or None,
                     time.monotonic() + ABORT_SETTLE,
                     interval=ABORT_POLL)
        wait_for(lambda: _tracking_standby(ctx, peer) or None,
                 time.monotonic() + ABORT_SETTLE, interval=ABORT_POLL)
    except Exception:
        pass


def _abort_verdict_pass(ctx, number, owner, peer, point, launch, want):
    """Submit behind a stalled command lane, abandon the wait, then audit
    the terminal settlement across both peers. Return the digest, violations,
    and captured evidence.
    """
    base = ctx[owner]
    label = 'qa-abort-verdict-' + str(number)
    command = {'write_value': {'point': point, 'kind': 'bool',
                               'value': {'bool': want}}}
    admission = {'command': command, 'actor': label}
    record = {'pass': number, 'owner': owner, 'peer': peer,
              'launch_roles': dict(launch),
              'admission': {'command': command, 'actor': label,
                            'value': want},
              'audit': {}}
    violations = {}

    def note(key, diagnostic, detail):
        violations.setdefault(key, (diagnostic, detail))

    def failed(key, detail):
        note(key, DIAG_FAILED, detail)

    def finish(result):
        record['violations'] = {key: {'diagnostic': name,
                                      'detail': detail}
                                for key, (name, detail)
                                in violations.items()}
        record['digest'] = result
        return result, violations, record

    # The journal floors the audit reads from — the served journals'
    # high-water at pass start.
    floors = {}
    try:
        for name in (owner, peer):
            floors[name] = _journal_cursor(ctx, ctx[name])
    except Exception as exc:
        failed('floors', 'a peer\'s journal floor never served: '
               + str(exc)[:200])
        return finish(None)
    record['floors'] = floors

    # The pin: a POST /command whose declared body arrives half-sent —
    # the reason pad pushes the Content-Length past tiny_http's eager
    # buffer so the rest stays on the socket and the one command
    # worker waits inside the body read while the abandoned post
    # lands. The pin's own write takes the opposite value, so an
    # applied image afterwards proves the buffered submission drained
    # last.
    stalled = json.dumps({
        'command': {'write_value': {'point': point, 'kind': 'bool',
                                    'value': {'bool': not want}}},
        'actor': label + '-pin',
        'reason': 'x' * STALLED_PAD}).encode()
    split = len(stalled) // 2
    completed = False
    try:
        stall = _connect(base)
        stall.sendall(b'POST /command HTTP/1.1\r\nHost: qa\r\n'
                      b'Content-Type: application/json\r\n'
                      b'Content-Length: '
                      + str(len(stalled)).encode()
                      + b'\r\n\r\n' + stalled[:split])
    except OSError as exc:
        failed('pin', 'the pinning connection never opened: '
               + str(exc)[:200])
        return finish(None)
    try:
        time.sleep(ABORT_LEAD)
        # The complete submission lands behind the pinned worker. Its read
        # bound must end before an answer, so the drain can prove a command
        # still settles after the client has stopped waiting.
        kind, payload = _post_command(
            base, {'command': command, 'actor': label}, ABORT_BOUND)
        record['submission'] = {'result': kind, 'answer': payload}
        if kind == 'answered':
            record['inconclusive'] = \
                'the pinned lane answered the abandoned post inside ' \
                'the tightened bound - the abort window never staged'
            return finish(None)
        if kind == 'refused':
            record['inconclusive'] = \
                'the abandoned post met an answered refusal inside ' \
                'the bound, so the abort window never staged'
            return finish(None)
        # The pinned window stays past the bound's firing before the
        # stalled body completes — the aborted request sits buffered
        # the whole time, exactly the backlog case the reproduction
        # names.
        time.sleep(ABORT_HOLD)
        stall.sendall(stalled[split:])
        completed = True
        record['pin_reply'] = _drain_reply(
            stall, time.monotonic() + ABORT_REPLY)
    finally:
        if not completed:
            # An aborted pass still releases the lane — the worker
            # stays pinned on the half-sent body otherwise.
            try:
                stall.sendall(stalled[split:])
            except OSError:
                pass
        stall.close()
    status, pin_receipt = record.get('pin_reply') or (None, None)
    if status != 200 or not isinstance(pin_receipt, dict):
        failed('pin', 'the released pin answered ' + str(status)
               + ' — the pinned lane never drained: '
               + json.dumps(pin_receipt)[:200])
        return finish(None)

    # The audit: one settle deadline covers the served-journal wait
    # and the durable-file wait together — the same scan boundary
    # lands both.
    journals = ctx.get('journal_files') or {}
    audit_deadline = time.monotonic() + ABORT_SETTLE

    def window():
        """One polled audit snapshot: each peer's journaled
        command_settled receipts matching the admission (served, since
        the pass's floors), its served receipt log's matching entries,
        and its served image value for the written point. None while a
        monitor drops a read — a lost observation, never the verdict."""
        found = {'journaled': {}, 'logged': {}, 'image': {}}
        for name in (owner, peer):
            try:
                _, journal = http_json('GET', ctx[name]
                                       + '/journal?since='
                                       + str(floors[name]))
                _, receipts = http_json('GET', ctx[name] + '/receipts')
                snapshot = _snapshot(ctx, ctx[name])
            except Exception:
                return None
            found['journaled'][name] = [
                receipt for receipt in
                (_journal_settled(entry)
                 for entry in _journal_list(journal))
                if _admission_hit(receipt, admission)]
            found['logged'][name] = [
                receipt for receipt in _receipt_list(receipts)
                if _admission_hit(receipt, admission)]
            found['image'][name] = _point_value(snapshot, point)
        return found

    def resolved(found):
        """Whether the snapshot is terminal for the admission: the
        settle journaled once per peer with the same outcome the
        served receipt logs carry — or a contradiction already
        showing, which no further wait can heal."""
        counts = {name: len(entries)
                  for name, entries in found['journaled'].items()}
        outcomes = {_outcome_key(receipt)
                    for entries in found['journaled'].values()
                    for receipt in entries}
        if len(outcomes) != 1:
            return bool(outcomes)   # contradiction is terminal;
                                    # silence keeps waiting
        if any(count > 1 for count in counts.values()):
            return True             # over-journaling is terminal too
        outcome = next(iter(outcomes))
        if any(_outcome_key(receipt) not in (outcome, 'accepted')
               for entries in found['logged'].values()
               for receipt in entries):
            return True             # an adopted log already
                                    # contradicts the journaled outcome
        return counts[owner] == 1 and counts[peer] == 1 \
            and all(len(found['logged'][name]) == 1
                    and _outcome_key(found['logged'][name][0])
                        == outcome
                    for name in (owner, peer))

    found = wait_for(
        lambda: (lambda w: w if w is not None and resolved(w)
                 else None)(window()),
        audit_deadline, interval=ABORT_POLL)
    if found is None:
        found = window() or {'journaled': {owner: [], peer: []},
                             'logged': {owner: [], peer: []},
                             'image': {owner: None, peer: None}}
    record['audit'] = {
        'journaled': {name: [_outcome_key(receipt)
                             for receipt in entries]
                      for name, entries in found['journaled'].items()},
        'logged': {name: [_outcome_key(receipt)
                          for receipt in entries]
                   for name, entries in found['logged'].items()},
        'image': dict(found['image'])}
    seen = {_outcome_key(receipt)
            for entries in found['journaled'].values()
            for receipt in entries}
    if len(seen) == 1:
        record['audit']['outcome'] = next(iter(seen))

    def file_window():
        """Each peer's durable --journal-file settle receipts matching
        the admission — None while a file cannot be read."""
        try:
            return {name: [receipt for receipt in
                           (_journal_settled(item)
                            for item in _journal_entries(
                                journals[name]))
                           if _admission_hit(receipt, admission)]
                    for name in (owner, peer)}
        except Exception:
            return None

    files = wait_for(
        lambda: (lambda w: w if w is not None
                 and all(len(entries) >= 1
                         for entries in w.values())
                 else None)(file_window()),
        audit_deadline, interval=ABORT_POLL)
    if files is None:
        files = file_window()
    record['audit']['files'] = {
        name: ([_outcome_key(receipt) for receipt in entries]
               if isinstance(entries, list) else None)
        for name, entries in (files or {}).items()}

    record['roles'] = {name: (_try_role(ctx, ctx[name]) or {})
                       .get('role') for name in (owner, peer)}

    problems = []

    def collect(key, diagnostic, detail):
        problems.append((key, diagnostic, detail))

    _judge_settle(record, collect)
    _judge_roles(record, collect)
    for key, diagnostic, detail in problems:
        note(key, diagnostic, detail)

    audit = record['audit']
    journaled = audit.get('journaled') or {}
    single = audit.get('outcome') is not None \
        and all(entries == [audit['outcome']]
                for entries in journaled.values()) \
        and len(journaled) == 2
    digest = {
        'receipt': audit.get('outcome') or 'unsettled',
        'journal': 'single' if single else 'diverged',
        'roles': 'held' if record['roles'] == launch else 'moved'}
    return finish(digest)


def scenario_command_abort_verdict(ctx):
    """Prove an abandoned command wait still reaches one settlement."""
    case = Case('command-abort-verdict',
                'Abandoned command wait preserves terminal settlement',
                'a command submitted behind a stalled command worker '
                'outlives its client read bound, then settles once in '
                'the served receipts, both served and durable journals, '
                'and corresponding images without moving launch roles; '
                'two passes produce identical digests and planted audit '
                'faults report their named diagnostics')
    launch = {}
    try:
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries only one endpoint — the pair '
                               'the cross-peer audit spans is absent')
        journals = ctx.get('journal_files') or {}
        if not journals.get('active') or not journals.get('standby'):
            return case.finish('inconclusive', 'the run context '
                               'carries no journal-file paths for the '
                               'deployed pair — the durable-journal '
                               'audit cannot run')
        deadline = time.monotonic() + ABORT_SETTLE
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=ABORT_POLL)
        if owner is None:
            reports = {name: _try_role(ctx, ctx[name])
                       for name in ('active', 'standby')
                       if ctx.get(name)}
            if not reports or all(report is None
                                  for report in reports.values()):
                return case.finish('inconclusive', 'the deployed pair '
                                   'is unreachable — monitor '
                                   'endpoints ' + str(ctx.get('active'))
                                   + ' and ' + str(ctx.get('standby')))
            return case.finish('failed', 'no peer reports role=active')
        peer = 'standby' if owner == 'active' else 'active'
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=ABORT_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the cross-peer '
                               'audit has no adopted line')
        launch = {owner: 'active', peer: 'standby'}
        case.observe('field owner: ' + owner + ' (' + ctx[owner]
                     + '); tracking peer: ' + peer + ' ('
                     + ctx[peer] + ')')
        _, signals = http_json('GET', ctx[owner] + '/signals')
        target = _writable_bool_point(signals)
        if target is None or target.get('point') is None:
            return case.finish('inconclusive', 'no writable bool '
                               'command point in the model')
        point = target['point']
        ref = save_evidence(ctx['evidence_dir'],
                            'command-abort-verdict-signals.json',
                            signals)
        case.evidence('file', ref, 'SignalIndex naming the aborted '
                      'submission\'s writable point')
        digests = []
        for number in (1, 2):
            digest, violations, record = _abort_verdict_pass(
                ctx, number, owner, peer, point, launch,
                want=number % 2 == 1)
            ref = save_evidence(
                ctx['evidence_dir'],
                'command-abort-verdict-pass-' + str(number) + '.json',
                record)
            case.evidence('file', ref, 'aborted-post pass '
                          + str(number) + ' — the '
                          'transport and settled-truth audit across '
                          'both peers, and the normalized digest')
            if record.get('inconclusive'):
                return case.finish('inconclusive',
                                   record['inconclusive'])
            if violations or digest is None:
                diagnostic = DIAG_FAILED \
                    if digest is None or any(
                        name == DIAG_FAILED
                        for name, _ in violations.values()) \
                    else DIAG_NONDET
                return case.finish(
                    'failed', diagnostic + ': ' + '; '.join(
                        detail for _, detail in
                        list(violations.values())[:4]))
            digests.append(digest)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', DIAG_NONDET + ': the two passes\' digests '
                'diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two aborted-post passes, identical digests: '
                     + json.dumps(digests[0], sort_keys=True))
        slipped = _self_check()
        if slipped:
            return case.finish('failed', DIAG_UNCHECKED
                               + ': planted negatives slipped the '
                               'leg\u2019s own audits: '
                               + ', '.join(slipped))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        _restore_launch_roles(ctx, launch)
