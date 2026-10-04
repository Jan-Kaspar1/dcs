"""The incumbent_consultation acceptance leg — one module per leg
of the scenario schedule; see qa_lane/scenarios/__init__.py for the
ordering rule and the shared seam."""
from .common import *

# Ordering: the leg drives one keyed pair through a full
# restart-as-active takeover cycle — the demote/promote switchover, a
# stop, two receipted tunes on the promoted incumbent, the refused
# restart, the killed incumbent, the granted one — and restores the
# pair's launch layout every pass. It therefore runs after the keyed
# announced-source leg whose verified announcements are what name a
# restartee's incumbent at all, and inside the claim-lifecycle cluster
# ahead of the revision legs that need every member as it launched.
RUNS_AFTER = frozenset({'scenario_keyed_announced_source'})
RUNS_BEFORE = frozenset({'scenario_incompatible_revision',
                         'scenario_model_revision'})


# --------------------------------------------------------------------
# The restartee-incumbent-consultation gate and its journaled takeover
# record — the per-revision lane evidence for the #735 fix
# (WW-LCM-001's runtime-tuning-continuity clause, which the gate
# exists to protect, and WW-OPS-003's named visibility of a takeover).
# Decision 98 makes a relaunched launched-active's startup claim
# two-gated: the field's arbitration keeps its decision-89 conditional
# grant, and a consultation of every incumbent endpoint the run can
# name becomes the takeover's own gate. The defect it closes is #735 —
# a restartee resumes its older persisted baseline and seizes the field
# while the promoted peer is alive, discarding every unit of run state
# the incumbent accumulated across the gap, and journals nothing about
# the takeover. Two halves, one staged cycle:
#
# - (a) The live incumbent. The pair is settled, ctrl-a is demoted and
#   ctrl-b promoted, ctrl-a is stopped, and two receipted parameter
#   tunes are applied on ctrl-b so ctrl-a's persisted baseline lags the
#   incumbent's run state by construction. Restarting ctrl-a must
#   refuse the startup claim *naming the consultation's verdict* — the
#   incumbent it asked answered — and ctrl-b must keep owning the
#   field with every applied value intact: the receipted tunes, its
#   held claim, its advancing tick. The refusal keeps decision 89's
#   `FieldClaimFailed` naming and the `--standby` remedy the record
#   preserves; what the gate adds is that the verdict read is the
#   consultation's, and that no adoption of the incumbent's checkpoint
#   stands in its place — the adopt-then-claim shape decision 98
#   declined.
#
# - (b) The unreachable incumbent. With ctrl-b stopped, the same
#   restart's consultation is unanswered — a dead incumbent is no
#   evidence and constrains nothing, which is exactly what keeps
#   decision 86's dead-owner restart and decision 94's unclaimed-wedge
#   recovery alive. The startup claim must therefore seize, and the
#   claiming line must owe the durable journal the recorded takeover
#   evidence: the resumed baseline (the persisted tick, generation, and
#   command-admission high-water, or the named cold start), the
#   consultation ledger (every address asked and its verdict), and the
#   arbitration's grant verdict. "Which baseline seized the field,
#   after asking whom, answered what" must be answerable from the
#   journal alone.
#
# Roles are restored after each pass: the seizing restartee keeps the
# field and the stopped incumbent comes back as its tracking standby.
# The seizing line's own roll-back of the gap's tunes is the recorded
# consequence of the dead-incumbent recovery, not a clause this leg
# asserts — the takeover record is what the decision requires of it.
#
# The subject is the lane's keyed pair (`_keyed_subject`, #1058): the
# deployed pair while the run config keys it, else the staged keyed
# probe pair. The consultation can only ask the addresses the
# deployment gave the run, so the restartee must actually name one —
# the leg reads it back out of ctrl-a's own served checkpoint before
# it stops, and reports inconclusive where none is stamped. An unkeyed
# posture is honest absence (decision 98's residual hole), never a
# failure.
#
# The recorded evidence is discovered *structurally*, not by a pinned
# variant name: the journal entry carrying a consultation ledger that
# names the consulted endpoint is the record whatever the build spells
# its fields. A staged release predating the gate journals its restart
# consult as one source plus a prose detail — never a ledger — and a
# granted startup claim carries no baseline beside it, so the leg
# reports inconclusive rather than inventing a contract it cannot read.
#
# Named diagnostics: incumbent-consultation-failed tags the contract
# clauses — a refusal that never lands or never names the
# consultation's verdict, a consultation adopted instead of refused, an
# incumbent disturbed or rolled back, a restart that does not seize
# over the dead one, a takeover record missing its baseline, its
# ledger, or its grant verdict, a resumed baseline that is not behind
# the incumbent's, a pair left un-restored — and
# incumbent-consultation-nondeterministic tags the instability the
# contract does not answer for: a refused staging call, a vanished
# container, an unsettled settle watch, two passes whose digests
# diverge. The unchecked-diagnostic self-check replays the judge over
# planted negatives and reports incumbent-consultation-unchecked for
# any that slip through.

CONSULT_SETTLE = 45     # bound on each settle/watch in the cycle
CONSULT_POLL = 0.4      # cadence polling a member mid-stage
CONSULT_TUNES = 2       # receipted tunes on the incumbent — the gap
                        # whose run state the resumed baseline lacks
# The refusal's own shape: decision 89's preserved naming.
REFUSAL_MARKER = 'write-ownership claim failed'
REMEDY_FRAGMENTS = ('--standby',)
# The phrasings a refusal naming the *consultation's* verdict may
# carry. The pre-gate message names the claim's live holder instead and
# never any of these; the verdict may equally name the consulted
# endpoint itself, which the leg also accepts.
CONSULT_VERDICT_FRAGMENTS = (
    'incumbent consultation', 'consulted incumbent',
    'consultation refused', 'refused the consultation',
    'incumbent holds the field', 'incumbent owns the field',
    'live incumbent', 'incumbent stands')
# A consultation verdict that affirms no incumbent — the refused
# takeover may not read as one of these.
ABSENT_VERDICT_FRAGMENTS = (
    'unanswered', 'unreachable', 'no answer', 'timeout', 'timed out',
    'not_own', 'not own', 'non-owning', 'non_owning',
    'unverifiable', 'unverified', 'cold')
# A grant verdict must affirm the grant — decision 89's refused shape
# never reads as a seizure.
REFUSED_GRANT_FRAGMENTS = ('refus', 'denied', 'fail', 'error')
# The structural vocabulary the recorded evidence is read through.
LEDGER_KEYS = ('consult', 'ledger', 'inquir', 'asked')
BASELINE_KEYS = ('resumed', 'baseline', 'superseded', 'cold')
GRANT_KEYS = ('grant', 'claim', 'claim_verdict')
ADDRESS_KEYS = ('addr', 'source', 'endpoint', 'peer', 'host')
DIAG_FAILED = 'incumbent-consultation-failed'
DIAG_NONDET = 'incumbent-consultation-nondeterministic'
DIAG_UNCHECKED = 'incumbent-consultation-unchecked'

MEMBERS = ('active', 'standby')


def _sync_key(report):
    """One report's convergence key — 'tracking', 'orphaned', ... —
    from either served spelling of the `sync` field."""
    sync = (report or {}).get('sync')
    if isinstance(sync, str):
        return sync
    if isinstance(sync, dict) and sync:
        return next(iter(sync))
    return None


def _view(report):
    """One endpoint's served evidence: role, convergence key, the
    observed field claim, and the served tick."""
    report = report or {}
    return {'role': report.get('role'), 'sync': _sync_key(report),
            'field_claim': report.get('field_claim'),
            'tick': report.get('tick')}


def _owning(view):
    """The endpoint's report while it owns the field's claim."""
    return (view or {}).get('role') == 'active' \
        and (view or {}).get('field_claim') == 'held'


def _converged(view):
    """The endpoint's report while it is a tracking standby on the
    pair's field owner."""
    return (view or {}).get('role') == 'standby' \
        and (view or {}).get('sync') == 'tracking'


def _advancing(before, after):
    """The endpoint kept scanning across the staged window."""
    return isinstance((before or {}).get('tick'), int) \
        and isinstance((after or {}).get('tick'), int) \
        and after['tick'] > before['tick']


def _consult_member_view(ctx, name):
    """One member's served surface, or None when its monitor does not
    answer — a dropped read is one lost sample, never the verdict."""
    base = ctx.get(name)
    if base is None:
        return None
    return _view(_try_role(ctx, base))


def _wait_view(ctx, name, want, deadline=None):
    """The member's report once it satisfies `want`, else the last
    report (or None) the bound produced — an unconverged member's own
    served surface is the evidence of what it reported instead."""
    latest = []

    def probe():
        view = _consult_member_view(ctx, name)
        if view is not None:
            latest.append(view)
        return view if view is not None and want(view) else None

    wait_for(probe, deadline or time.monotonic() + CONSULT_SETTLE,
             interval=CONSULT_POLL)
    return latest[-1] if latest else None


def _wait_state(ctx, name, stopped, deadline=None):
    """A member container's process verdict once it reads `stopped`,
    else the last verdict the bound produced."""
    latest = []

    def probe():
        state = ctx['controller_state'](name)
        latest.append(state)
        if state.get('absent'):
            return state if stopped else None
        return state if (not state.get('running')) == stopped else None

    wait_for(probe, deadline or time.monotonic() + CONSULT_SETTLE,
             interval=CONSULT_POLL)
    return latest[-1] if latest else None


def _refusal_line(logs):
    """The refusal line a refused startup prints — the run's own
    `error: field write-ownership claim failed: ...` line, the shape
    decision 98 keeps for the consultation's verdict."""
    for line in (logs or '').splitlines():
        if REFUSAL_MARKER in line:
            return line.strip()
    return ''


def _names_verdict(text, consulted):
    """Whether the refusal line names the consultation's verdict — the
    consulted endpoint itself or one of the recorded phrasings — and
    which fragment matched."""
    if not text:
        return None
    if consulted and consulted in text:
        return 'the consulted endpoint ' + str(consulted)
    lowered = text.lower()
    for fragment in CONSULT_VERDICT_FRAGMENTS:
        if fragment in lowered:
            return fragment
    return None


def _pairs(node, depth=6):
    """[(lowercased field name, value)] for every field under `node`,
    outermost first — the flatten the recorded evidence's fields are
    read through, so no variant spelling is pinned."""
    found = []
    if depth <= 0:
        return found
    if isinstance(node, dict):
        for name, value in node.items():
            found.append((str(name).lower(), value))
            found.extend(_pairs(value, depth - 1))
    elif isinstance(node, list):
        for value in node:
            found.extend(_pairs(value, depth - 1))
    return found


def _nodes(payload, keys):
    """Every dict node under `payload` carrying a field name that
    mentions one of `keys`."""
    found = []
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if any(any(key in name for key in keys) for name in node):
                found.append(node)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


def _int_field(node, keys):
    """The first integer under a field naming one of `keys`."""
    for name, value in _pairs(node):
        if any(key in name for key in keys) \
                and isinstance(value, int) \
                and not isinstance(value, bool):
            return value
    return None


def _str_field(node, keys):
    """The first string under a field naming one of `keys`."""
    for name, value in _pairs(node):
        if any(key in name for key in keys) and isinstance(value, str):
            return value
    return None


def _ledger_entry(item):
    """(address, verdict) out of one consultation-ledger entry — a
    record carrying both, or a bare address naming no verdict."""
    if isinstance(item, str):
        return item, None
    if not isinstance(item, dict):
        return None, None
    strings = [(name, value) for name, value in _pairs(item)
               if isinstance(value, str)]
    address = next((value for _name, value in strings if ':' in value),
                   None)
    if address is None:
        address = next((value for name, value in strings
                        if any(key in name for key in ADDRESS_KEYS)), None)
    verdict = next((value for _name, value in strings
                    if value != address), None)
    return address, verdict


def _ledger_of(payload):
    """[(address, verdict)] — the consultation ledger: the list a
    payload carries under a ledger-named field, each entry's asked
    endpoint paired with the verdict that entry carries."""
    for node in _nodes(payload, LEDGER_KEYS):
        for name, value in node.items():
            if not isinstance(value, list) or not value:
                continue
            if not any(key in str(name).lower() for key in LEDGER_KEYS):
                continue
            entries = [_ledger_entry(item) for item in value]
            if any(address for address, _verdict in entries):
                return entries
    return []


def _baseline_of(payload):
    """The resumed baseline a takeover record carries: the persisted
    tick, generation, and command-admission high-water, or the named
    cold start. None where the payload carries no baseline."""
    for node in _nodes(payload, BASELINE_KEYS):
        for name, value in node.items():
            if isinstance(value, str) \
                    and any(key in str(name).lower()
                            for key in BASELINE_KEYS):
                return {'tick': None, 'generation': None,
                        'attempts': None, 'cold': value}
        entry = {'tick': _int_field(node, ('tick',)),
                 'generation': _int_field(node, ('generation',)),
                 'attempts': _int_field(node, ('attempts', 'admission')),
                 'cold': _str_field(node, ('cold',))}
        if entry['cold'] or all(
                entry[key] is not None
                for key in ('tick', 'generation', 'attempts')):
            return entry
    return None


def _grant_field(name):
    """Whether a field name reads as the arbitration's verdict on the
    startup claim — a `grant`-named field, or a claim-verdict-named
    one. Never a ledger entry's own verdict, and never the `claimant`
    an attribution record names."""
    return 'grant' in name or ('claim' in name and 'verdict' in name)


def _grant_of(payload):
    """The arbitration's grant verdict as the record spells it — the
    string, or the boolean read as a verdict, under a grant-named
    field, descending one level into a grant-named record."""
    for node in _nodes(payload, GRANT_KEYS):
        for name, value in _pairs(node):
            if not _grant_field(name):
                continue
            if isinstance(value, str):
                return value
            if isinstance(value, bool):
                return 'granted' if value else 'refused'
        for name, value in _pairs(node):
            if not _grant_field(name):
                continue
            verdict = _str_field(value, ('verdict', 'result', 'outcome',
                                         'grant'))
            if verdict:
                return verdict
    return None


def _same_endpoint(left, right):
    """Whether a ledger entry names the consulted endpoint — the exact
    address, or the in-container monitor port every pair member's
    surface binds in the rig."""
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    if left == right:
        return True
    port = left.rpartition(':')[2]
    return bool(port) and port == right.rpartition(':')[2]


def _consult_records(entries, consulted):
    """[(event name, facts)] for every journaled payload carrying a
    consultation ledger that names `consulted` — the recorded
    consultation evidence decision 98 owes the durable journal,
    whatever variant and field names the build spells it with. Empty
    where the staged release predates the gate: a pre-gate consult
    entry carries one source and a prose detail, never a ledger."""
    found = []
    for item in entries or []:
        event = (item.get('entry') or {}).get('event') or {}
        for name, payload in event.items():
            if not isinstance(payload, dict):
                continue
            ledger = _ledger_of(payload)
            if not ledger:
                continue
            asked = [(address, verdict) for address, verdict in ledger
                     if _same_endpoint(address, consulted)]
            if not asked:
                continue
            found.append((name, {
                'ledger': asked,
                'baseline': _baseline_of(payload),
                'grant': _grant_of(payload),
                'adopted': 'adopt' in json.dumps(payload).lower()}))
    return found


def _lifetime_entries(path):
    """The `--journal-file` records of its final process lifetime — the
    entries after the last run-boundary marker, which is the lifetime
    whose startup claim the recorded evidence belongs to. None where
    the file is unreadable."""
    if not path or not Path(path).is_file():
        return None
    records = _journal_entries(path)
    last = -1
    for index, record in enumerate(records):
        if 'run_boundary' in record:
            last = index
    return records[last + 1:]


def _journal_boundaries(path):
    """[{run, tick}] every run-boundary marker the `--journal-file`
    carries — the lifetimes a restart opened and the tick each resumed
    at. None where the file is unreadable."""
    if not path or not Path(path).is_file():
        return None
    marks = []
    for record in _journal_entries(path):
        if 'run_boundary' in record:
            body = record['run_boundary'] or {}
            marks.append({'run': body.get('run'),
                          'tick': body.get('tick')})
    return marks


def _resumed_baseline(path):
    """The persisted checkpoint's own baseline — the resumed tick,
    generation, and command-admission high-water a `--state-file`
    restart resumes, read host-side out of the runner-owned state
    file. None where the file is absent or unreadable."""
    if not path or not Path(path).is_file():
        return None
    try:
        body = json.loads(Path(path).read_text())
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    admission = body.get('command_admission') or {}
    return {'tick': body.get('tick'),
            'generation': body.get('generation'),
            'attempts': admission.get('attempts')
            if isinstance(admission, dict) else None,
            'source': body.get('tracking_source')}


def _tune(ctx, base, component, name, value, number):
    """One receipted parameter tune through the ordinary command path —
    the run state across the gap that the restartee's older baseline
    must not discard. The submission's absolute index and its settled
    verdict come back, so the pass can show the admission high-water it
    moved."""
    command = {'command': {'set_parameter': {
        'component': component, 'name': name,
        'value': {'float': value}}}, 'actor': 'qa-lane'}
    index = _next_receipt_index(ctx, base)
    status, receipt = http_json('POST', base + '/command', command)
    settled = wait_for(lambda: _settled_outcome(ctx, base, index),
                       time.monotonic() + CONSULT_SETTLE,
                       interval=CONSULT_POLL)
    return {'command': command, 'status': status, 'index': index,
            'outcome': _outcome_key(receipt if isinstance(receipt, dict)
                                    else {}),
            'settled': settled, 'number': number}


def _switchover(ctx):
    """(demote, promote) the pair's deliberate switchover: the
    launched active steps down and the converged standby takes the
    field. A refused or failed call returns its status for the pass to
    stage as an instability, never a contract verdict."""
    demote = _settle_call(ctx['active'] + '/demote')
    promote = None
    deadline = time.monotonic() + CONSULT_SETTLE
    while time.monotonic() < deadline:
        status, body = _settle_call(ctx['standby'] + '/promote')
        if status == 200:
            promote = (status, body)
            break
        time.sleep(CONSULT_POLL)
    return demote, promote or (None, None)


def _restore_pair(ctx, record):
    """The subject pair's launch layout after a pass: both members
    running again, one owning the field and the other converged on it.
    The restore runs on every exit path — a refused start, a staged
    exception, a judged contract miss — so no pass leaves the pair
    dismantled for the legs behind it."""
    members = {}
    for name in MEMBERS:
        try:
            state = ctx['controller_state'](name)
        except Exception as exc:
            members[name] = 'state read refused: ' + str(exc)[:150]
            continue
        if (state or {}).get('running') is not True:
            try:
                ctx['start_controller'](name)
            except Exception as exc:
                members[name] = 'start refused: ' + str(exc)[:150]
                continue
        members[name] = None
    def settled(view):
        return _owning(view) or _converged(view)

    active = _wait_view(ctx, 'active', settled)
    standby = _wait_view(ctx, 'standby', settled)
    owner = active if _owning(active) else standby
    tracker = standby if owner is active else active
    record['restored'] = {
        'members': members, 'active': active, 'standby': standby,
        'settled': _owning(owner) and _converged(tracker)
        and not any(members.values())}


def _consult_pass(ctx, number):
    """One pass over the consultation-gate contract on one keyed pair:
    switch the field onto ctrl-b, stop ctrl-a, tune the incumbent so
    the resumed baseline lags, restart ctrl-a over the live incumbent
    and read its refusal, then kill the incumbent and restart ctrl-a
    again to read the granted seizure's recorded evidence, restoring
    the pair's launch layout. Returns (record, evidence); the judge
    replays the record."""
    record = {'pass': number}
    evidence = {'pass': number}
    before = {name: _consult_member_view(ctx, name) for name in MEMBERS}
    record['before'] = before
    if not _owning(before.get('active')) \
            or not _converged(before.get('standby')):
        evidence['inconclusive'] = (
            'the subject pair is not in the settled launch shape the '
            'cycle needs — the launched active must own the field with '
            'its member converged on it: '
            + json.dumps(before, sort_keys=True)[:200])
        return record, evidence
    try:
        # The switchover, and the address the restartee's consult will
        # ask: read back out of ctrl-a's own served checkpoint while it
        # stands demoted and converged, so the pass never has to
        # synthesize the rig's in-container addressing.
        demote, promote = _switchover(ctx)
        record['switchover'] = {'demote': demote[0],
                                'promote': promote[0]}
        if demote[0] != 200 or promote[0] != 200:
            record['stage_error'] = (
                'the switchover never completed: demote '
                + str(demote[0]) + ' promote ' + str(promote[0]))
            _restore_pair(ctx, record)
            return record, evidence
        demoted = _wait_view(ctx, 'active', _converged)
        incumbent = _wait_view(ctx, 'standby', _owning)
        record['demoted'] = demoted
        record['incumbent'] = incumbent
        if not _converged(demoted) or not _owning(incumbent):
            record['stage_error'] = (
                'the switchover never settled — the demoted peer reads '
                + json.dumps(demoted, sort_keys=True)[:120]
                + ' and the promoted '
                + json.dumps(incumbent, sort_keys=True)[:120])
            _restore_pair(ctx, record)
            return record, evidence
        _, served = http_json('GET', ctx['active'] + '/checkpoint')
        consulted = (served or {}).get('tracking_source')
        record['consulted'] = consulted
        record['resumed_file'] = _resumed_baseline(
            (ctx.get('state_files') or {}).get('active'))
        if not consulted:
            evidence['inconclusive'] = (
                'the demoted peer names no incumbent checkpoint stream, '
                'so a restart of it can consult nothing — decision 98 '
                'records this as the honest residual hole (a restartee '
                'asks only the addresses the deployment gave it): '
                'configure --peer, or run the pair keyed')
            _restore_pair(ctx, record)
            return record, evidence
        ctx['stop_controller']('active')
        record['stopped'] = _wait_state(ctx, 'active', True)
        if (record['stopped'] or {}).get('running'):
            record['stage_error'] = ('the restartee never went down — '
                                     'the staging void')
            _restore_pair(ctx, record)
            return record, evidence

        # The receipted tunes on the incumbent: the run state across
        # the gap the restartee's persisted baseline must not carry,
        # and the admission high-water it must lag.
        _, schema = http_json('GET', ctx['standby'] + '/schema')
        plan = _float_tune_plan(
            schema, _try_snapshot(ctx, ctx['standby']) or {})
        if plan is None:
            evidence['inconclusive'] = (
                'no descriptor-declared Float parameter is served, so '
                'the gap the restartee\'s baseline must lag cannot be '
                'staged')
            _restore_pair(ctx, record)
            return record, evidence
        component, name, current, tuned, _outside = plan
        record['tune_target'] = {'component': component, 'name': name,
                                 'was': current, 'tuned': tuned}
        record['tunes'] = [
            _tune(ctx, ctx['standby'], component, name, tuned, index + 1)
            for index in range(CONSULT_TUNES)]
        record['incumbent_before_restart'] = _consult_member_view(ctx, 'standby')
        record['incumbent_tick'] = (
            record['incumbent_before_restart'] or {}).get('tick')
        record['tuned_before'] = _parameter_value(
            _try_snapshot(ctx, ctx['standby']) or {}, component, name)

        # (a) The live incumbent: the restart-as-active claim must
        # refuse, naming the consultation's verdict, and leave the
        # incumbent owning the field with every applied value intact.
        ctx['restart_controller']('active')
        record['refused'] = _wait_state(ctx, 'active', True)
        line = _refusal_line((record['refused'] or {}).get('logs') or '')
        record['refusal'] = {'line': line[:400],
                             'verdict': _names_verdict(line, consulted)}
        record['refusal_records'] = _consult_records(
            _lifetime_entries(_journal(ctx, 'active')), consulted)
        record['incumbent_after_restart'] = _consult_member_view(ctx, 'standby')
        record['incumbent_tuned'] = _parameter_value(
            _try_snapshot(ctx, ctx['standby']) or {}, component, name)

        # (b) The unreachable incumbent: the same restart's
        # consultation is unanswered, the claim seizes, and the
        # claiming line owes the journal the recorded evidence.
        ctx['stop_controller']('standby')
        record['incumbent_down'] = _wait_state(ctx, 'standby', True)
        ctx['start_controller']('active')
        record['seized'] = _wait_view(ctx, 'active', _owning)
        record['seized_state'] = _wait_state(ctx, 'active', False)
        entries = _lifetime_entries(_journal(ctx, 'active'))
        records = _consult_records(entries, consulted)
        record['takeover_records'] = records
        record['takeover'] = next(
            (facts for _name, facts in records
             if facts['baseline'] is not None
             and facts['grant'] is not None), None)
        record['boundaries'] = _journal_boundaries(_journal(ctx, 'active'))
    except Exception as exc:
        record['stage_error'] = str(exc)[:300]
    _restore_pair(ctx, record)
    return record, evidence


def _journal(ctx, name):
    """A member's runner-owned `--journal-file` path."""
    return (ctx.get('journal_files') or {}).get(name)


def _pre_gate_release(record):
    """The staged-release reason, or None where the pass carries the
    contract's recorded evidence. Neither recorded record is the
    discriminator: a build predating the gate journals its consult as
    one source and a prose detail and grants a startup claim with no
    baseline beside it, so the journal carries neither half — while a
    build that records one and misses the other has landed the gate
    halfway, which is the judge's contract miss rather than this
    predicate's honest absence. A pass that never staged is the
    judge's instability to name, not this predicate's."""
    if record.get('stage_error') is not None:
        return None
    if record.get('refusal_records') or record.get('takeover'):
        return None
    return ('the durable journal carries neither a consultation ledger '
            'naming the consulted incumbent for the refused start nor '
            'a takeover record carrying the baseline, the ledger, and '
            'the grant verdict for the granted startup claim — the '
            "staged release predates decision 98's recorded "
            'consultation evidence, so the gate it installs cannot be '
            'exercised on this revision')


def _judge_consultation(record, note):
    """Audit one pass's record — replayable, so the self-check can hand
    it planted negatives. `note(key, diagnostic, detail)` records each
    clause the record violates: DIAG_FAILED tags the contract clauses
    and DIAG_NONDET the instability the contract does not answer
    for."""
    def failed(key, detail):
        note(key, DIAG_FAILED, detail)

    def nondet(key, detail):
        note(key, DIAG_NONDET, detail)

    if record.get('stage_error') is not None:
        nondet('stage', 'the staging never completed: '
               + str(record['stage_error']))
        return
    consulted = record.get('consulted')
    resumed = record.get('resumed_file') or {}
    tunes = record.get('tunes') or []
    target = record.get('tune_target') or {}

    # (a) The live incumbent.
    refused = record.get('refused') or {}
    refusal = record.get('refusal') or {}
    if refused.get('absent'):
        nondet('refusal-vanished', 'the restartee\'s container '
               'vanished — no process verdict exists to audit')
    elif refused.get('running'):
        nondet('refusal-unsettled', 'the restart-as-active never '
               'settled a verdict inside the bound: it still runs '
               + json.dumps(refused)[:150])
    elif not refused.get('exit'):
        failed('refusal-exit', 'the restart-as-active exited zero over '
               'a reachable live incumbent: '
               + json.dumps(refused)[:200])
    elif not refusal.get('line'):
        failed('refusal-named', 'the refusal never named the startup '
               'claim\'s failure: '
               + str(refused.get('logs') or '')[-200:])
    else:
        logs = refused.get('logs') or ''
        if not all(fragment in logs for fragment in REMEDY_FRAGMENTS):
            failed('refusal-remedy', 'the refusal never named the '
                   'relaunch remedy the record preserves: ' + logs[-200:])
        if not refusal.get('verdict'):
            failed('refusal-verdict', 'the refusal named the claim\'s '
                   'live holder, not the consultation\'s verdict: '
                   + str(refusal.get('line'))[:200])
    records = record.get('refusal_records') or []
    if not records:
        failed('refusal-ledger', 'the refused start journaled no '
               'consultation ledger naming ' + str(consulted) + ' — '
               'the incumbent answer that refused the start is the '
               'audit the takeover owes')
    for name, facts in records:
        if facts['adopted']:
            failed('refusal-adopted', 'the refused start adopted the '
                   'incumbent\'s checkpoint (' + str(name) + ') instead '
                   'of refusing on the consultation — the '
                   'adopt-then-claim shape the record declined')
        if any(any(absent in (verdict or '').lower()
                   for absent in ABSENT_VERDICT_FRAGMENTS)
               for _address, verdict in facts['ledger']):
            failed('refusal-verdict-record', 'the refused start\'s '
                   'consultation ledger reports no incumbent for the '
                   'address it asked: '
                   + json.dumps(facts['ledger'])[:250])
    incumbent = record.get('incumbent') or {}
    after = record.get('incumbent_after_restart') or {}
    if not _owning(after):
        failed('incumbent-ownership', 'the live incumbent lost the '
               'field to the refused restart: ' + json.dumps(after)[:200])
    elif not _advancing(incumbent, after):
        failed('incumbent-scan', 'the live incumbent stopped scanning '
               'across the refused restart: '
               + json.dumps({'before': incumbent,
                             'after': after})[:250])
    if record.get('incumbent_tuned') != target.get('tuned'):
        failed('incumbent-tunes', 'the live incumbent\'s applied tune '
               'did not survive the refused restart — reads '
               + str(record.get('incumbent_tuned')) + ' where the '
               'receipted tune settled ' + str(target.get('tuned'))
               + ': the gap\'s run state was rolled back by the seizure '
               'this gate exists to prevent')

    # (b) The unreachable incumbent.
    if (record.get('incumbent_down') or {}).get('running'):
        failed('incumbent-down', 'the unreachable incumbent never went '
               'down — its consultation could not answer unanswered: '
               + json.dumps(record.get('incumbent_down'))[:200])
    if not _owning(record.get('seized')):
        failed('seize', 'the restart-as-active never seized the field '
               'over the dead incumbent: '
               + json.dumps(record.get('seized'))[:200])
    if (record.get('seized_state') or {}).get('exit'):
        failed('seize-exit', 'the seizing restartee exited instead of '
               'holding the field: '
               + json.dumps(record.get('seized_state'))[:200])
    takeover = record.get('takeover')
    if takeover is None:
        failed('takeover-record', 'the granted startup claim journaled '
               'no takeover record carrying the baseline, the ledger, '
               'and the grant verdict: '
               + json.dumps(record.get('takeover_records'))[:300])
        return
    if takeover['baseline'] is None:
        failed('takeover-baseline', 'the takeover record carries no '
               'resumed baseline — the tick, generation, and admission '
               'high-water the seizure claimed, or the named cold '
               'start: ' + json.dumps(takeover)[:250])
    if not any(any(absent in (verdict or '').lower()
                   for absent in ABSENT_VERDICT_FRAGMENTS)
               for _address, verdict in takeover['ledger']):
        failed('takeover-ledger', 'the takeover record\'s ledger '
               'reports an answered consultation for the dead '
               'incumbent: ' + json.dumps(takeover['ledger'])[:250])
    grant = (takeover.get('grant') or '').lower()
    if not grant:
        failed('takeover-grant', 'the takeover record carries no '
               'arbitration grant verdict: ' + json.dumps(takeover)[:250])
    elif any(refused_word in grant
             for refused_word in REFUSED_GRANT_FRAGMENTS):
        failed('takeover-grant-verdict', 'the takeover record\'s grant '
               'verdict reads ' + repr(takeover['grant']) + ' — a '
               'granted startup claim owes the grant that fired')
    baseline = takeover.get('baseline') or {}
    if baseline.get('cold') is None and resumed:
        for key in ('tick', 'generation', 'attempts'):
            if resumed.get(key) is not None \
                    and baseline.get(key) is not None \
                    and baseline[key] != resumed[key]:
                failed('takeover-baseline-stamp', 'the takeover '
                       'record\'s resumed ' + key + ' reads '
                       + str(baseline[key]) + ' where the persisted '
                       'checkpoint the restart resumed stood at '
                       + str(resumed[key]))
        ahead = record.get('incumbent_tick')
        if isinstance(resumed.get('tick'), int) \
                and isinstance(ahead, int) and resumed['tick'] >= ahead:
            failed('baseline-lag', 'the restartee\'s resumed baseline '
                   'stands at tick ' + str(resumed['tick'])
                   + ', not behind the incumbent\'s last served '
                   + str(ahead) + ' — the staged gap the seizure had '
                   'to discard never existed')
    if any(tune.get('settled') != 'applied' for tune in tunes):
        failed('tunes', 'a receipted tune on the incumbent never '
               'settled applied, so the gap\'s run state was never '
               'staged: ' + json.dumps(tunes)[:250])
    if not (record.get('restored') or {}).get('settled'):
        failed('restore', 'the subject pair was not restored to its '
               'launch layout — both members running, one owning the '
               'field and the other converged on it: '
               + json.dumps(record.get('restored'))[:300])


def _consult_digest(record, violations):
    """The pass's normalized verdict record — identical across clean
    passes; each field carries the recorded disposition only while no
    violation names it."""
    def clean(*keys):
        return not any(key in violations for key in keys)

    staged = record.get('stage_error') is None

    def disposition(recorded, held, *keys):
        """One field's normalized disposition: 'unrecorded' where the
        pass staged no evidence for it at all, the recorded
        disposition while no violation names it, else 'defect'."""
        if not recorded:
            return 'unrecorded'
        return held if staged and clean(*keys) else 'defect'

    return {
        'live-incumbent': disposition(
            bool(record.get('refusal_records')), 'refused-named',
            'refusal-vanished', 'refusal-unsettled', 'refusal-exit',
            'refusal-named', 'refusal-remedy', 'refusal-verdict',
            'refusal-ledger', 'refusal-adopted', 'refusal-verdict-record'),
        'incumbent': 'undisturbed'
            if clean('incumbent-ownership', 'incumbent-scan',
                     'incumbent-tunes') else 'disturbed',
        'unreachable-incumbent': disposition(
            bool(record.get('takeover_records')), 'seized',
            'seize', 'seize-exit'),
        'takeover-evidence': disposition(
            bool(record.get('takeover')), 'baseline+ledger+grant',
            'takeover-record', 'takeover-baseline', 'takeover-ledger',
            'takeover-grant', 'takeover-grant-verdict',
            'takeover-baseline-stamp'),
        'resumed-baseline': 'lagging'
            if clean('baseline-lag', 'tunes',
                     'takeover-baseline-stamp') else 'defect',
        'pair-restored': 'settled'
            if clean('restore', 'stage') else 'disturbed'}


def _consult_clean_record():
    """The pass record the contract holds on every clause: the switch
    settled, the restartee named its incumbent, two receipted tunes
    landed, the live-incumbent restart refused naming the consultation
    verdict and journaled the ledger, the incumbent kept the field and
    its tunes, and the dead-incumbent restart seized and journaled the
    recorded takeover evidence. The self-check plants its negatives by
    mutating this."""
    return {
        'pass': 1,
        'consulted': '10.9.9.2:8081',
        'resumed_file': {'tick': 120, 'generation': 4, 'attempts': 6,
                         'source': '10.9.9.2:8081'},
        'tune_target': {'component': 'pump-group-1',
                        'name': 'start_level', 'was': 0.4,
                        'tuned': 0.5},
        'tunes': [{'status': 200, 'index': 6, 'outcome': 'accepted',
                   'settled': 'applied', 'number': 1},
                  {'status': 200, 'index': 7, 'outcome': 'accepted',
                   'settled': 'applied', 'number': 2}],
        'incumbent': {'role': 'active', 'sync': None,
                      'field_claim': 'held', 'tick': 200},
        'incumbent_before_restart': {'role': 'active', 'sync': None,
                                     'field_claim': 'held', 'tick': 300},
        'incumbent_tick': 380,
        'tuned_before': 0.5,
        'incumbent_after_restart': {'role': 'active', 'sync': None,
                                    'field_claim': 'held', 'tick': 420},
        'incumbent_tuned': 0.5,
        'incumbent_down': {'container': 'dcs-hw-qa-1-b',
                           'running': False, 'exit': 0, 'logs': '',
                           'absent': False},
        'refused': {'container': 'dcs-hw-qa-1-a', 'running': False,
                    'exit': 1, 'absent': False,
                    'logs': 'error: field write-ownership claim '
                            'failed: the incumbent consultation at '
                            '10.9.9.2:8081 reports a live owner — '
                            'relaunch with --standby ADDRESS to rejoin '
                            'as its tracking standby instead'},
        'refusal': {'line': 'error: field write-ownership claim '
                           'failed: the incumbent consultation at '
                           '10.9.9.2:8081 reports a live owner — '
                           'relaunch with --standby ADDRESS',
                    'verdict': 'the consulted endpoint 10.9.9.2:8081'},
        'refusal_records': [('restart_consultation', {
            'ledger': [('10.9.9.2:8081', 'live_incumbent')],
            'baseline': {'tick': 120, 'generation': 4, 'attempts': 6,
                         'cold': None},
            'grant': 'refused', 'adopted': False})],
        'seized': {'role': 'active', 'sync': None,
                   'field_claim': 'held', 'tick': 500},
        'seized_state': {'container': 'dcs-hw-qa-1-a', 'running': True,
                         'exit': None, 'logs': '', 'absent': False},
        'takeover_records': [('startup_takeover', {
            'ledger': [('10.9.9.2:8081', 'unanswered')],
            'baseline': {'tick': 120, 'generation': 4, 'attempts': 6,
                         'cold': None},
            'grant': 'granted', 'adopted': False})],
        'takeover': {'ledger': [('10.9.9.2:8081', 'unanswered')],
                     'baseline': {'tick': 120, 'generation': 4,
                                  'attempts': 6, 'cold': None},
                     'grant': 'granted', 'adopted': False},
        'boundaries': [{'run': 1, 'tick': 0}, {'run': 2, 'tick': 120}],
        'restored': {'members': {'active': None, 'standby': None},
                     'active': {'role': 'active', 'sync': None,
                                'field_claim': 'held', 'tick': 900},
                     'standby': {'role': 'standby', 'sync': 'tracking',
                                 'field_claim': 'held', 'tick': 880},
                     'settled': True}}


def _consult_self_check():
    """The leg's unchecked-diagnostic self-test: replay the judge over
    each planted negative — every recorded failure shape must name
    DIAG_FAILED, the instability shapes DIAG_NONDET, and the clean
    record neither. Returns the negative names the judge let through."""
    slipped = []

    def judge(record):
        found = {}
        _judge_consultation(record, lambda key, diagnostic, detail:
               found.setdefault(key, diagnostic))
        return found

    def expect(name, mutate, diagnostic=DIAG_FAILED):
        record = _consult_clean_record()
        mutate(record)
        if diagnostic not in judge(record).values():
            slipped.append(name)

    flagged = judge(_consult_clean_record())
    if flagged:
        slipped.append('clean-record-flagged:' + ','.join(
            sorted(flagged)))

    # The doctored negatives: the unnamed refusal, the declined
    # adoption, the disturbed or rolled-back incumbent, the seized-or-
    # not-over-the-dead restart, the incomplete takeover record, the
    # misstated baseline, and the unrestored pair.
    expect('refusal-container-vanished', lambda record:
           record['refused'].update({'exit': None, 'absent': True}),
           DIAG_NONDET)
    expect('refusal-never-settles', lambda record:
           record['refused'].update({'running': True, 'exit': None}),
           DIAG_NONDET)
    expect('refusal-exit-zero', lambda record:
           record['refused'].update({'exit': 0}))
    expect('refusal-unnamed', lambda record:
           record['refusal'].update({'line': '', 'verdict': None}))
    expect('refusal-no-remedy', lambda record:
           record['refused'].update(
               {'logs': 'error: field write-ownership claim failed: '
                        'the incumbent consultation at 10.9.9.2:8081 '
                        'reports a live owner'}))
    expect('refusal-verdict-unnamed', lambda record:
           record['refusal'].update(
               {'verdict': None,
                'line': 'error: field write-ownership claim failed: a '
                        'live peer holds the field\'s '
                        'write-ownership claim — relaunch with '
                        '--standby ADDRESS'}))
    expect('refusal-ledger-missing', lambda record:
           record.update({'refusal_records': []}))
    expect('refusal-adopted', lambda record:
           record['refusal_records'][0][1].update({'adopted': True}))
    expect('refusal-verdict-nonowning', lambda record:
           record['refusal_records'][0][1].update(
               {'ledger': [('10.9.9.2:8081', 'does_not_own')]}))
    expect('incumbent-demoted', lambda record:
           record['incumbent_after_restart'].update(
               {'role': 'standby', 'field_claim': None}))
    expect('incumbent-stalled', lambda record:
           record['incumbent_after_restart'].update({'tick': 200}))
    expect('incumbent-tunes-rolled-back', lambda record:
           record.update({'incumbent_tuned': 0.4}))
    expect('incumbent-never-down', lambda record:
           record['incumbent_down'].update({'running': True}))
    expect('seize-refused', lambda record:
           record.update({'seized': {'role': 'standby',
                                     'sync': 'tracking',
                                     'field_claim': 'held'}}))
    expect('seize-exited', lambda record:
           record['seized_state'].update({'exit': 1, 'running': False}))
    expect('takeover-record-missing', lambda record:
           record.update({'takeover': None, 'takeover_records': []}))
    expect('takeover-baseline-missing', lambda record:
           record['takeover'].update({'baseline': None}))
    expect('takeover-ledger-answered', lambda record:
           record['takeover'].update(
               {'ledger': [('10.9.9.2:8081', 'live_incumbent')]}))
    expect('takeover-grant-missing', lambda record:
           record['takeover'].update({'grant': None}))
    expect('takeover-grant-refused', lambda record:
           record['takeover'].update({'grant': 'refused'}))
    expect('takeover-baseline-misstated', lambda record:
           record['takeover']['baseline'].update({'generation': 9}))
    expect('baseline-not-lagging', lambda record:
           record['resumed_file'].update({'tick': 500}))
    expect('tune-unsettled', lambda record:
           record['tunes'][1].update({'settled': None}))
    expect('pair-unrestored', lambda record:
           record['restored'].update({'settled': False}))
    expect('stage-refused', lambda record:
           record.update({'stage_error': 'docker stop failed'}),
           DIAG_NONDET)
    return slipped


def scenario_incumbent_consultation(ctx):
    """Exercise the restartee-incumbent-consultation gate and its
    journaled takeover record on one keyed pair: a restart-as-active
    over a reachable live incumbent refuses naming the consultation's
    verdict and leaves the incumbent owning the field with its
    receipted tunes intact, a restart-as-active over an unreachable
    incumbent seizes and journals the resumed baseline, the
    consultation ledger, and the grant verdict, the pair's launch
    layout is restored, and two consecutive passes produce identical
    digests."""
    case = Case(
        'incumbent-consultation',
        'A restartee consults a reachable incumbent before its startup '
        'claim fires, and journals the takeover evidence',
        'a restart-as-active over a reachable live incumbent refuses '
        'the startup claim naming the consultation verdict and leaves '
        'the incumbent owning the field with its receipted tunes '
        'intact; a restart-as-active over an unreachable incumbent '
        'seizes and journals the resumed baseline, the consultation '
        'ledger, and the grant verdict; the pair is restored afterward '
        'and two passes produce identical digests')
    try:
        subject = _keyed_subject(ctx)
        if subject is None:
            return case.finish('inconclusive', 'the run stages no keyed '
                               'pair — the announced-source basis the '
                               'consultation reads is keyed-only by '
                               'contract')
        missing = [key for key in (
            'stop_controller', 'start_controller', 'restart_controller',
            'controller_state') if subject.get(key) is None]
        if missing:
            return case.finish('inconclusive', 'the run context carries '
                               'no controller-lifecycle levers for the '
                               'consultation staging: '
                               + ', '.join(missing))
        if not all(subject.get(name) for name in MEMBERS):
            return case.finish('inconclusive', 'the run context carries '
                               'no published monitor for the subject '
                               'pair')
        journals = subject.get('journal_files') or {}
        if not all(journals.get(name) for name in MEMBERS):
            return case.finish('inconclusive', 'the run context carries '
                               'no per-member journal files — the '
                               'durable half of the audit cannot run')

        digests = []
        for number in (1, 2):
            violations = {}

            def note(key, diagnostic, detail):
                violations.setdefault(key, (diagnostic, detail))

            record, evidence = _consult_pass(subject, number)
            evidence['record'] = record
            if not evidence.get('inconclusive'):
                evidence['inconclusive'] = _pre_gate_release(record)
            if not evidence.get('inconclusive'):
                _judge_consultation(record, note)
            digest = _consult_digest(record, violations)
            evidence['digest'] = dict(digest)
            evidence['violations'] = {
                key: diagnostic for key, (diagnostic, _)
                in violations.items()}
            ref = save_evidence(
                subject['evidence_dir'],
                'incumbent-consultation-pass-' + str(number) + '.json',
                evidence)
            case.evidence('file', ref,
                          'incumbent-consultation pass ' + str(number)
                          + ' — the switchover, the consulted address, '
                          'the resumed baseline, the receipted tunes, '
                          'the refused start, the unreachable-incumbent '
                          'seizure, both lifetimes\' journaled '
                          'consultation evidence, the restore, and the '
                          'normalized digest')
            if evidence.get('inconclusive'):
                return case.finish('inconclusive',
                                   evidence['inconclusive'])
            if violations:
                name = DIAG_FAILED if any(
                    diagnostic == DIAG_FAILED
                    for diagnostic, _ in violations.values()) \
                    else DIAG_NONDET
                return case.finish(
                    'failed', name + ': ' + '; '.join(
                        detail for _, detail in
                        list(violations.values())[:4]))
            digests.append(digest)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', DIAG_NONDET + ': the two passes\' digests '
                'diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two consultation-gate passes, identical digests: '
                     + json.dumps(digests[0], sort_keys=True))

        # The unchecked-diagnostic self-check: the judge replays each
        # planted negative it must name; a silent judge means the leg
        # can no longer catch what it names.
        slipped = _consult_self_check()
        if slipped:
            return case.finish('failed', DIAG_UNCHECKED + ': planted '
                               'negatives slipped the leg’s own '
                               'audits: ' + ', '.join(slipped))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))