"""The command_abort_verdict acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the abort-verdict leg rides the same command-lane pin the
# ordering leg's labeled wave proves — it sits directly behind it in
# the settled-pair window.
RUNS_AFTER = frozenset({'scenario_command_overflow_order'})


# --------------------------------------------------------------------
# The aborted bounded-command honest-verdict contract (WW-LCM-001's
# receipt-as-truth clause, decision 83's bounded command admission —
# the contract #1036's fix established, exercised per-revision for the
# defect whose bounded post path reported "command failed" for a
# command the controller could still apply): a bounded postCommand
# whose client-side wait aborts — timed out, disconnected, or
# cancelled — ends only the client's wait, never the server's work, so
# the submission's fate stays unresolved and the only honest
# post-facing report is the indeterminate "outcome unknown" verdict.
# The settled receipt stays the command's only truth: the served
# /receipts and the journaled command_settled carry the true terminal
# verdict — applied or the named refusal — and exactly one settle
# stands per admission across both peers.
#
# The mechanics mirror the unit reproduction's transport stand-in: the
# single command worker pins on a stalled head — a POST /command whose
# declared body arrives half-sent — so a second labeled submission
# lands buffered in the lane while no answer can return. The client's
# tightened bound ends the wait unanswered — the page's
# AbortSignal.timeout firing — and the mirrored submitCommand mints
# the indeterminate verdict rather than the defect's failure claim.
# Completing the stalled body drains the lane: the buffered command
# mints, applies at its scan boundary, and the settle journals on the
# field owner and the tracking peer's adopted line alike. The audit:
# the report is the indeterminate verdict — never "command failed" —
# the served receipt logs and each peer's durable --journal-file carry
# the admission's one terminal outcome, the served journals hold
# exactly one command_settled for it per peer, and the pair's launch
# roles stand. Named diagnostics are command-abort-verdict-failed for
# a contract miss — the pinned lane never staged, the post reported
# failure, no terminal verdict journaled, the durable record missing —
# command-abort-verdict-nondeterministic for an outcome the contract
# declares impossible — two settles on one admission, peers
# contradicting the verdict, a moved role, two passes disagreeing —
# and command-abort-verdict-unchecked when the self-check's planted
# negatives slip the leg's own audits. A staged run predating the
# contract — a served page without the honest-verdict surface — or
# without journal-file paths reports inconclusive.

DIAG_FAILED = 'command-abort-verdict-failed'
DIAG_NONDET = 'command-abort-verdict-nondeterministic'
DIAG_UNCHECKED = 'command-abort-verdict-unchecked'

ABORT_LEAD = 0.6       # the pin's lead — the command worker pops the
                       # stalled head before the abandoned post lands
ABORT_HOLD = 0.8       # the pinned window the abort rides inside —
                       # longer than the post's tightened bound
ABORT_BOUND = 0.3      # the abandoned post's tightened client bound —
                       # the page's POLL_MS abort stand-in: it ends the
                       # wait, never the server's work
ABORT_SETTLE = 30      # bound on the pair settling, the journaled
                       # settles landing, and each best-effort restore
ABORT_REPLY = 15       # bound on the pin's answer draining after the
                       # stalled body completes
ABORT_POLL = 0.25      # wait cadence inside the leg
STALLED_PAD = 2048     # the reason pad pushing the pinning body's
                       # Content-Length past tiny_http's eager buffer
PAGE_TIMEOUT = 5       # the served-page contract probe's bound
PAGE_MARKERS = ('abandonedSubmission', 'indeterminate')


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
    """The page's `postCommand`, mirrored on the raw transport: one
    POST /command under the tightened client bound — the socket's own
    connect/send/read bounds standing in for `AbortSignal.timeout`.
    Returns ('answered', receipt) for the in-bound 2xx answer,
    ('refused', (status, body)) for the endpoint's own answered 4xx —
    the one end that proves the command never queued — and
    ('unanswered', detail) for every end without an answered response:
    the bound firing, a dead response path, or a refused connect, all
    indistinguishable at the post's surface."""
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


def _submit_command(base, body, bound):
    """The page's `submitCommand` POST leg plus `abandonedSubmission`,
    mirrored: the answered receipt renders as itself, the endpoint's
    own answered refusal is the sole honest 'command failed', and
    every unanswered end mints the indeterminate verdict — the
    page-minted `{"indeterminate": ...}` outcome plus the pending
    notice the journaled settle resolves — never a failure claim for
    a fate the unanswered post left open."""
    kind, payload = _post_command(base, body, bound)
    if kind == 'answered':
        return {'verdict': 'receipt', 'receipt': payload}
    if kind == 'refused':
        status, detail = payload
        return {'verdict': 'failed', 'refused': True,
                'detail': 'command failed: the monitor refused the '
                          'request: HTTP ' + str(status) + ': '
                          + json.dumps(detail)[:200]}
    return {'verdict': 'indeterminate',
            'answer': {'command': body.get('command'),
                       'actor': body.get('actor'),
                       'outcome': {'indeterminate': {'detail': payload}}},
            'notice': 'command outcome unknown — no receipt answered '
                      'the submission (' + str(payload) + ') and it '
                      'may still apply: '
                      + json.dumps(body.get('command'), sort_keys=True)
                      + '. The journaled settled receipt is the '
                      'verdict — resubmitting now risks applying the '
                      'command twice.'}


def _judge_report(record, note):
    """The aborted post's report audit: the staged unanswered post
    must surface the indeterminate verdict — the honest
    submitted/unknown answer — never 'command failed' for a command
    the controller may still apply."""
    report = record.get('report') or {}
    verdict = report.get('verdict')
    if verdict == 'failed':
        note('report-failed', DIAG_FAILED,
             'the aborted bounded post reported \'command failed\' '
             '— ' + str(report.get('detail'))[:160] + ' — while the '
             'submission\'s fate stayed unresolved: the defect the '
             'honest-verdict contract exists to prevent')
        return
    if verdict != 'indeterminate':
        note('report-verdict', DIAG_FAILED,
             'the aborted bounded post answered '
             + json.dumps(verdict) + ' — never the indeterminate '
             'verdict the honest contract mints for an unanswered '
             'submission')
        return
    answer = report.get('answer') or {}
    outcome = answer.get('outcome') or {}
    if 'indeterminate' not in outcome:
        note('report-outcome', DIAG_FAILED,
             'the abandoned post\'s answer carries no indeterminate '
             'outcome: ' + json.dumps(answer)[:200])
    notice = report.get('notice') or ''
    if 'command failed' in notice \
            or 'may still apply' not in notice \
            or 'settled receipt' not in notice:
        note('report-notice', DIAG_FAILED,
             'the abandoned post\'s notice is not the honest verdict '
             'text — it must state the command may still apply and '
             'name the journaled settled receipt the verdict: '
             + notice[:200])


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
    """The leg's unchecked-diagnostic self-test: replay each judge
    over the planted negatives it must name — the aborted submission
    asserted 'command failed' while the journal settled it applied,
    a report that skipped the indeterminate mint, a settle that never
    journaled or journaled twice, a peer contradicting the verdict,
    an adopted log still pending, a durable record missing the
    settle, a phantom image write, and a moved role — and require
    each to trip. A silent judge returns the negative names it let
    through."""
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
                'report': {'verdict': 'indeterminate',
                           'answer': {'command': command,
                                      'actor': 'qa-abort-verdict-1',
                                      'outcome': {'indeterminate': {
                                          'detail': 'the read bound '
                                                  'fired'}}},
                           'notice': 'command outcome unknown — no '
                                     'receipt answered the submission '
                                     '(the read bound fired) and it '
                                     'may still apply: the journaled '
                                     'settled receipt is the verdict '
                                     '— resubmitting now risks '
                                     'applying the command twice.'},
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
            _judge_report(record, note)
            _judge_settle(record, note)
            _judge_roles(record, note)
        return judge

    def expect(name, judge):
        found = []
        judge(lambda key, diagnostic, detail: found.append(key))
        if not found:
            slipped.append(name)

    # The doctored negative the finding names: the aborted submission
    # asserted 'command failed' while the journaled settle stands
    # applied.
    record = clean()
    record['report'] = {'verdict': 'failed',
                        'detail': "command failed: "
                                  "TimeoutError('the read bound "
                                  "fired')"}
    expect('report-failed-while-applied', every(record))
    # An answered post standing in for the abort — the report that
    # skipped the indeterminate mint entirely.
    record = clean()
    record['report'] = {'verdict': 'receipt',
                        'receipt': {'outcome': {'applied': {'tick': 4}}}}
    expect('answered-verdict', every(record))
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
    """One aborted-post pass: the command worker pinned on a stalled
    head, the labeled writable-point submission landing behind it
    under the tightened bound — unanswered by construction — the
    mirrored submitCommand report, then the pin's release and the
    settled-truth audit across both peers. Returns (digest,
    violations, record): digest is the pass's normalized verdict —
    identical across clean passes; violations maps clause keys to
    (diagnostic, detail) in first-seen order."""
    base = ctx[owner]
    label = 'qa-abort-verdict-' + str(number)
    command = {'write_value': {'point': point, 'kind': 'bool',
                               'value': {'bool': want}}}
    admission = {'command': command, 'actor': label}
    record = {'pass': number, 'owner': owner, 'peer': peer,
              'launch_roles': dict(launch),
              'admission': {'command': command, 'actor': label,
                            'value': want},
              'report': None, 'audit': {}}
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
        # The abandoned bounded post: the labeled submission lands in
        # the lane behind the pinned worker — sent in full, answered
        # never inside the tightened bound — and the mirrored
        # submitCommand must mint the indeterminate verdict, never
        # 'command failed'.
        report = _submit_command(
            base, {'command': command, 'actor': label}, ABORT_BOUND)
        record['report'] = report
        if report['verdict'] == 'receipt':
            record['inconclusive'] = \
                'the pinned lane answered the abandoned post inside ' \
                'the tightened bound — the abort window never staged'
            return finish(None)
        if report.get('refused'):
            # An answered 4xx is the one provable refusal — 'command
            # failed' is the honest verdict for it and the may-apply
            # case the leg stages never formed.
            record['inconclusive'] = \
                'the abandoned post met an answered refusal inside ' \
                'the bound — the command provably never queued, so ' \
                'the may-apply case never staged'
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

    _judge_report(record, collect)
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
        'report': (record.get('report') or {}).get('verdict')
                  or 'unstaged',
        'receipt': audit.get('outcome') or 'unsettled',
        'journal': 'single' if single else 'diverged',
        'roles': 'held' if record['roles'] == launch else 'moved'}
    return finish(digest)


def scenario_command_abort_verdict(ctx):
    """Exercise the aborted bounded-command honest-verdict contract on
    the deployed pair: pin the field owner's single command worker on
    a stalled head, land a labeled writable-point submission behind it
    under a tightened client bound — the post's wait ends unanswered —
    and prove the post-facing report is the indeterminate verdict,
    never 'command failed', while the served /receipts and the durable
    journal carry the admission's true terminal verdict and exactly
    one command_settled stands for it across both peers."""
    case = Case('command-abort-verdict',
                'Aborted bounded command reports the honest verdict',
                'with the deployed pair settled, a receipted '
                'writable-point command submitted on the field owner '
                'through the bounded post path — the single command '
                'worker pinned on a stalled head so the tightened '
                'client bound ends the wait unanswered — reports the '
                'indeterminate outcome rather than \'command failed\', '
                'the served /receipts and the durable journal carry '
                'the admission\'s true terminal verdict (applied or '
                'the named refusal), exactly one command_settled '
                'stands for the admission across both peers, and the '
                'pair\'s launch roles are restored; two consecutive '
                'passes produce identical digests and the self-check '
                'leg\'s planted negatives each report their named '
                'diagnostic')
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
        # The contract gate: the served page must carry the
        # honest-verdict surface — the indeterminate-outcome
        # vocabulary and the abandoned-submission mint — a staged run
        # predating #1036's fix serves the page that reported
        # 'command failed' beside a landed write, and the leg reports
        # inconclusive rather than a verdict.
        try:
            status, raw = _request_status('GET', ctx[owner] + '/',
                                          timeout=PAGE_TIMEOUT)
        except Exception as exc:
            return case.finish('inconclusive', owner + '\'s page '
                               'never answered: ' + str(exc)[:200]
                               + ' — the staged run predates the '
                               'honest-verdict contract')
        text = raw.decode(errors='replace') \
            if isinstance(raw, bytes) else str(raw or '')
        missing = [marker for marker in PAGE_MARKERS
                   if marker not in text]
        if status != 200 or missing:
            return case.finish('inconclusive', 'the served page '
                               'lacks the honest-verdict surface ('
                               + ', '.join(missing
                                           or ['HTTP ' + str(status)])
                               + ') — the staged run predates the '
                               'aborted-command verdict contract')
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
                          + str(number) + ' — the post-facing '
                          'report, the settled-truth audit across '
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
