"""The tracker_realign_tick_order leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the leg stages the launch-layout pair — the unconfigured
# field owner frozen mid-run so its armed tracking peer's pulls
# produce nothing — and restores the launch roles by construction (a
# freeze held inside the armed failover budget never moves a role),
# so it sits in the launch-layout window the announced-source cluster
# keeps, behind the rediscovery leg whose own restore leaves the same
# layout standing.
RUNS_AFTER = frozenset({'scenario_track_source_rediscovery'})
RUNS_BEFORE = frozenset({'scenario_failover'})


# --------------------------------------------------------------------
# The tracking-realign journal tick-order contract — the per-revision
# lane evidence for #830's fix (WW-FND-004's attributed durable-record
# clause): a tracking peer that realigns its tick domain after a
# degraded window must stamp journaled entries in tick order — the
# durable journal's recorded tick never regresses within a run, so
# attribution stays ordered for consumers. The defect had the peer's
# durable journal stamping entries out of tick order after realigning:
# the frozen source left the tracker's own run clock pacing ahead while
# its degraded scans journaled the stale marks at that held tick, and
# the realigning apply's covering checkpoint carried a settled receipt
# whose line-apply tick sat below the standing mark — the adopted
# `command_settled` journaled at the carried tick and the journal's
# axis rewound.
#
# The leg stages the defect's own shape through the runner's
# frozen-source lever — the source-partition the orphan and
# bounded-liveness legs already drive: `docker pause` on the field
# owner holds its monitor socket open but unanswered, so the tracking
# peer's in-flight checkpoint fetches drop to produced-nothing misses
# and the served sync verdict goes degraded while the peer's scan —
# and its journaled stale-transition traffic — runs ahead of the line
# it last pulled. A receipted write submitted on the owner just ahead
# of the freeze settles while the pulls produce nothing, so the
# checkpoint the resumed pull applies carries the settled receipt for
# the peer to adopt — the journaled `command_settled` whose stamp is
# exactly the carried-tick surface the defect rewound. The audit then
# reads the peer's durable --journal-file: within the run, every
# entry's recorded tick must be at or after its predecessor's.
#
# Named diagnostics: realign-tick-order-failed tags the contract
# clauses — a journal entry stamping a tick below an earlier entry's
# within the run, a malformed tick-axis record, the settled receipt
# the covering checkpoint carries never journaling on the peer (or
# journaling twice), the peer never reconverging onto resumed pulls,
# the launch roles unrestored — and
# realign-tick-order-nondeterministic tags the instability the
# contract does not answer for: a refused staging call, a starved
# watch, a partition that never produced the degraded window, the
# armed failover firing inside the held window, the admission
# journaled ahead of the partition, a window that journaled no
# transition evidence ahead of the adopted settle. A staged run that
# predates the contract's durable-record surface — no per-controller
# journal file, or records with no integer tick axis to audit —
# reports inconclusive. The unchecked-diagnostic self-check replays
# the judge over planted negatives and reports
# realign-tick-order-unchecked for any that slip through.

ORDER_SETTLE = 45    # bound on each settle/reconverge/restore wait
ORDER_POLL = 0.15    # cadence watching the tracking peer mid-window
ORDER_HOLD = 4.0     # hard bound on the frozen-source window — dozens
                     # of produced-nothing pulls at the 100 ms scan
                     # cadence, far under the armed failover budget
ORDER_ADOPT = 30     # bound on the covering checkpoint's adopted
                     # settle landing in the peer's durable journal
ORDER_ACTOR = 'qa-realign-tick-order'
DIAG_FAILED = 'realign-tick-order-failed'
DIAG_NONDET = 'realign-tick-order-nondeterministic'
DIAG_UNCHECKED = 'realign-tick-order-unchecked'


def _order_row(ctx, name):
    """One normalized watch row off the tracking peer's served /role —
    the observation the held-window audit replays: the reported role,
    the sync verdict's kind, and the armed failover's miss accounting.
    None when the peer does not answer — a dropped observation, never
    a row."""
    report = _try_role(ctx, ctx[name])
    if report is None:
        return None
    sync = report.get('sync')
    kind = sync if isinstance(sync, str) else 'none'
    if isinstance(sync, dict) and sync:
        kind = next(iter(sync))
    failover = report.get('failover') or {}
    return {'role': report.get('role'), 'sync': kind,
            'misses': failover.get('misses'),
            'budget': failover.get('budget')}


def _gained_entries(path, floor):
    """The durable `entry` records the peer's --journal-file carries
    since `floor` records — file order is append order, the axis the
    audit walks."""
    out = []
    for item in _journal_entries(path)[floor:]:
        entry = item.get('entry')
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _adopted_settles(entries, command, actor):
    """The `command_settled` entries a gained tail journals for the
    leg's admission — the covering checkpoint's carried receipt the
    realign lands — matched on the (command, actor) pair the shared
    seam pins."""
    admission = {'command': command, 'actor': actor}
    return [entry for entry in entries
            if _admission_hit(_journal_settled(entry) or {},
                              admission)]


def _apply_tick(entry):
    """The line-apply tick an adopted settle's receipt carries — the
    source-domain stamp the journal axis must absorb rather than
    rewind to."""
    outcome = (_journal_settled(entry) or {}).get('outcome') or {}
    applied = outcome.get('applied')
    return applied.get('tick') if isinstance(applied, dict) else None


def _axis_audit(path):
    """The current run's durable tick-axis audit over the peer's
    --journal-file: the records after the last `run_boundary` marker
    (the whole file when none has landed yet), each `entry` record's
    integer tick held against the running mark — the non-regressing
    append axis the #830 fix's clamped stamp owes. Returns
    {'entries': n, 'regressions': [...], 'malformed': n} — a malformed
    count names the records carrying no integer tick at all."""
    entries = []
    malformed = 0
    for item in _journal_entries(path):
        if 'run_boundary' in item:
            entries = []
            malformed = 0
            continue
        entry = item.get('entry')
        if not isinstance(entry, dict):
            continue
        tick = entry.get('tick')
        if not isinstance(tick, int) or isinstance(tick, bool):
            malformed += 1
            continue
        entries.append({'seq': entry.get('seq'), 'tick': tick})
    regressions = []
    mark = None
    for entry in entries:
        if mark is not None and entry['tick'] < mark:
            regressions.append({'seq': entry.get('seq'),
                                'tick': entry['tick'],
                                'below': mark})
        else:
            mark = entry['tick']
    return {'entries': len(entries), 'regressions': regressions,
            'malformed': malformed}


def _judge_tick_order(record, note):
    """Audit one pass's record — replayable, so the self-check can
    hand it planted negatives. `note(key, diagnostic, detail)`
    records each clause the record violates: DIAG_FAILED tags the
    tick-order contract clauses — the journaled axis regressing, a
    durable record carrying no integer tick, the covering
    checkpoint's settled receipt unjournaled or double-journaled on
    the peer, the reconvergence that never came, the launch roles
    unrestored — and DIAG_NONDET tags the instability the contract
    does not answer for: a refused staging call, a starved watch, a
    partition that never produced the degraded verdict, the armed
    failover firing inside the held window, the admission settling
    into the tracking stream ahead of the partition, a window that
    journaled no transition ahead of the adopted settle. An aborted
    stage ends the audit where the pass ended — the later keys it
    never wrote are not clauses."""
    def failed(key, detail):
        note(key, DIAG_FAILED, detail)

    def nondet(key, detail):
        note(key, DIAG_NONDET, detail)

    if record.get('journal_error') is not None:
        nondet('journal-read', 'the peer\'s durable journal could '
               'not be read: ' + str(record['journal_error']))
        return
    if record.get('pause_error') is not None:
        nondet('partition', 'the frozen-source induction never '
               'landed: ' + str(record['pause_error']))
        return
    if record.get('unpause_error') is not None:
        nondet('partition', 'the frozen source was never thawed — '
               'the realign could not stage: '
               + str(record['unpause_error']))
        return
    baseline = record.get('baseline')
    if not isinstance(baseline, dict) or baseline.get('role') \
            != 'standby' or baseline.get('sync') != 'tracking':
        nondet('watch', 'the pass never observed the tracking '
               'posture it stages from: '
               + json.dumps(baseline)[:200])
    admission = record.get('admission') or {}
    settled = record.get('settled')
    applied = (settled or {}).get('outcome') == 'applied'
    if admission.get('status') != 200:
        nondet('admission', 'the induction command\'s submission '
               'answered ' + str(admission.get('status')) + ' '
               + str(admission.get('error'))[:160]
               + ' — the settled receipt the covering checkpoint '
               'must carry was never admitted')
        applied = False
    elif not applied:
        nondet('admission', 'the induction command settled '
               + json.dumps((settled or {}).get('outcome'))
               + ' on the field owner, not applied — the covering '
               'checkpoint carries no applied receipt to adopt')
    rows = record.get('window') or []
    if not rows:
        nondet('watch', 'the held window collected no served rows — '
               'the starved monitor gave the audit nothing to read')
    armed = False
    if record.get('failover') is not None:
        armed = True
        nondet('armed-window', 'the tracking peer left its standby '
               'role inside the frozen-source window — the armed '
               'failover boundary was reached inside the calibrated '
               'hold: ' + json.dumps(record['failover'])[:200])
    if not isinstance(record.get('degraded'), dict):
        nondet('degraded', 'the frozen source never produced the '
               'degraded verdict — the produced-nothing pulls the '
               'partition owes never landed')
    if not armed and record.get('realigned') is None:
        failed('reconverge', 'the tracking peer never reconverged '
               'to tracking on the resumed pulls — the realign the '
               'axis audit is staged across never landed: last row '
               + json.dumps(rows[-1] if rows else None)[:200])
    adopted = record.get('adopted') or []
    if not armed and record.get('realigned') is not None and applied:
        if len(adopted) != 1:
            failed('adopted', 'the covering checkpoint\'s settled '
                   'receipt journaled ' + str(len(adopted))
                   + ' command_settled records on the peer instead '
                   'of exactly one — the durable record the realign '
                   'lands is '
                   + ('absent' if not adopted else 'repeated'))
        elif not record.get('window_marks'):
            nondet('adoption-window', 'the adopted settle leads the '
                   'gained tail — either it journaled ahead of the '
                   'partition or the degraded window produced no '
                   'journaled transition ahead of it, so the '
                   'held-axis the audit needs was never staged')
    axis = record.get('axis') or {}
    if axis.get('malformed'):
        failed('axis-shape', 'the durable journal holds '
               + str(axis['malformed']) + ' entry records carrying '
               'no integer tick — the attributed-record axis the '
               'contract pins is malformed')
    if axis.get('regressions'):
        failed('axis', 'the durable journal\'s tick axis regressed '
               'across the realign: '
               + json.dumps(axis['regressions'][:4])[:300])
    if not record.get('restored') and not armed:
        failed('roles', 'the pair never settled back to its launch '
               'roles — ' + str(record.get('owner')) + ' active '
               'with ' + str(record.get('peer'))
               + ' tracking behind it')


def _tick_order_digest(violations):
    """The pass's normalized verdict record — identical across clean
    passes; each field is the clean value only while no violation —
    contract or instability — names its clause."""
    def clean(*keys):
        return not any(key in violations for key in keys)
    return {
        'admission': 'settled' if clean('admission') else 'unstaged',
        'window': 'degraded' if clean('degraded', 'watch')
            else 'unproven',
        'realigned': 'tracking' if clean('reconverge')
            else 'stranded',
        'adopted': 'journaled'
            if clean('adopted', 'adoption-window') else 'absent',
        'axis': 'ordered' if clean('axis', 'axis-shape')
            else 'regressed',
        'roles': 'restored' if clean('roles', 'armed-window')
            else 'unrestored'}


def _tick_order_self_check():
    """The leg's unchecked-diagnostic self-test: replay the tick-order
    judge over each planted negative the issue names — the journal
    asserted as ordered while an entry stamps a regressed tick, a
    malformed axis record, the adopted settle absent or repeated, the
    reconvergence that never came, the launch roles unrestored — and
    require the judge to note each; the instability shapes must
    report nondeterministic, not failed. A silent judge returns the
    negative names it let through."""
    slipped = []

    def clean_record():
        return {'owner': 'active', 'peer': 'standby',
                'baseline': {'role': 'standby', 'sync': 'tracking',
                             'misses': 0, 'budget': 120},
                'admission': {'status': 200, 'index': 3, 'point': 10},
                'settled': {'outcome': 'applied', 'apply_tick': 101},
                'window': [
                    {'role': 'standby', 'sync': 'tracking',
                     'misses': 0, 'budget': 120},
                    {'role': 'standby', 'sync': 'degraded',
                     'misses': 7, 'budget': 120},
                    {'role': 'standby', 'sync': 'degraded',
                     'misses': 14, 'budget': 120}],
                'degraded': {'role': 'standby', 'sync': 'degraded',
                             'misses': 7, 'budget': 120},
                'failover': None,
                'unpaused': True,
                'realigned': {'role': 'standby',
                              'sync': {'tracking': {'aligned': 118}}},
                'adopted': [{'seq': 9, 'tick': 120,
                             'apply_tick': 101}],
                'window_marks': 1,
                'axis': {'entries': 9, 'regressions': [],
                         'malformed': 0},
                'restored': {'role': 'standby',
                             'sync': {'tracking': {'aligned': 124}}}}

    def expect(name, mutate, diagnostic=DIAG_FAILED):
        record = clean_record()
        mutate(record)
        found = {}
        _judge_tick_order(
            record,
            lambda key, diag, detail: found.setdefault(key, diag))
        if diagnostic not in found.values():
            slipped.append(name)

    # The doctored negative the issue names first: the journal
    # asserted as ordered while an entry stamps a tick below an
    # earlier entry's within the run.
    expect('axis-regressed', lambda record: record['axis'].update(
        {'regressions': [{'seq': 9, 'tick': 101, 'below': 118}]}))
    # ... and the axis record malformed — an entry carrying no
    # integer tick.
    expect('axis-malformed', lambda record: record['axis'].update(
        {'malformed': 1}))
    # The covering checkpoint's settled receipt never journaled on
    # the peer — and journaled twice.
    expect('adopted-silent', lambda record: record.update(
        {'adopted': []}))
    expect('adopted-twice', lambda record: record.update(
        {'adopted': [{'seq': 9, 'tick': 120, 'apply_tick': 101},
                     {'seq': 10, 'tick': 121, 'apply_tick': 101}]}))
    # The reconvergence never came.
    expect('never-realigned', lambda record:
           record.update({'realigned': None}))
    # The launch roles never restored.
    expect('roles-unrestored', lambda record:
           record.update({'restored': None}))
    # The instability the contract does not answer for must report
    # nondeterministic: a refused staging call, a dropped durable
    # read, a starved watch, the partition that never produced the
    # degraded verdict, the armed failover firing inside the held
    # window, the admission unapplied, and the adopted settle
    # landing ahead of the window's transition evidence.
    expect('pause-refused', lambda record:
           record.update({'pause_error': 'docker pause failed'}),
           DIAG_NONDET)
    expect('journal-unreadable', lambda record: record.update(
        {'journal_error': 'journal file does not parse'}),
        DIAG_NONDET)
    expect('watch-starved', lambda record:
           record.update({'window': []}), DIAG_NONDET)
    expect('never-degraded', lambda record:
           record.update({'degraded': None}), DIAG_NONDET)
    expect('failover-fired', lambda record: record.update(
        {'failover': {'role': 'promoting', 'sync': 'none',
                      'misses': 120, 'budget': 120},
         'realigned': None, 'restored': None}), DIAG_NONDET)
    expect('admission-refused', lambda record: record.update(
        {'admission': {'status': 409, 'error': 'queue_full'}}),
        DIAG_NONDET)
    expect('settle-unapplied', lambda record: record.update(
        {'settled': {'outcome': 'rejected:superseded'}}),
        DIAG_NONDET)
    expect('adopted-early', lambda record:
           record.update({'window_marks': 0}), DIAG_NONDET)
    return slipped


def _restore_launch(ctx, owner, peer):
    """Best-effort launch-layout restore on the deployed pair: thaw a
    frozen source, then walk the pair back to the launch roles — the
    named owner holding the field, the sibling tracking behind it.
    Every step is retried inside the bound and swallowed on
    refusal."""
    try:
        ctx['unpause_controller'](owner)
    except Exception:
        pass
    try:
        if (_try_role(ctx, ctx[peer]) or {}).get('role') \
                in ('active', 'promoting'):
            _settle_call(ctx[peer] + '/demote')
        deadline = time.monotonic() + ORDER_SETTLE
        while time.monotonic() < deadline:
            if (_try_role(ctx, ctx[owner]) or {}).get('role') \
                    != 'active':
                _settle_call(ctx[owner] + '/promote')
            if _pair_active(ctx) == owner \
                    and _tracking_standby(ctx, peer) is not None:
                return
            time.sleep(ORDER_POLL)
    except Exception:
        pass


def _tick_order_pass(ctx, number, owner, peer):
    """One realign pass: with the pair settled and tracking, submit
    the receipted induction write on the field owner, freeze the
    owner's container so the tracking peer's pulls produce nothing —
    the degraded window whose journaled transitions hold the run's
    tick axis ahead of the line — then thaw so the covering
    checkpoint realigns the peer and lands the settled receipt for
    adoption. The audit reads the peer's durable journal: the gained
    entries' transition traffic and the run's tick axis. Returns
    (record, evidence): the record is what the judge replays; an
    aborted stage simply leaves its later keys absent for the judge
    to name."""
    record = {'owner': owner, 'peer': peer}
    evidence = {'pass': number, 'owner': owner, 'peer': peer}
    base, peer_base = ctx[owner], ctx[peer]
    journal_path = ctx['journal_files'][peer]

    try:
        floor = len(_journal_entries(journal_path))
    except Exception as exc:
        record['journal_error'] = str(exc)[:300]
        return record, evidence

    record['baseline'] = _order_row(ctx, peer)

    # The induction admission: a receipted point write on the field
    # owner whose settle the covering checkpoint carries to the
    # realigning peer — the carried-apply-tick record the defect
    # stamped below the standing mark. A refused submission is
    # staged around: the frozen-source window and the axis audit
    # still run and the judge names the admission it never saw
    # settle rather than the contract clauses a half-staged pass
    # cannot speak for.
    command = None
    index = None
    try:
        _, signals = http_json('GET', base + '/signals')
        points = _writable_bool_points(signals, 1)
        if not points:
            record['admission'] = {'status': None,
                                   'error': 'no writable bool point'}
        else:
            command = {'write_value': {'point': points[0],
                                       'kind': 'bool',
                                       'value': {'bool':
                                                 bool(number % 2)}}}
            index = _next_receipt_index(ctx, base)
            status, _body = http_json(
                'POST', base + '/command',
                {'command': command, 'actor': ORDER_ACTOR})
            record['admission'] = {'status': status, 'index': index,
                                   'point': points[0]}
    except Exception as exc:
        record['admission'] = {'status': None,
                               'error': str(exc)[:200]}

    # The source partition: the paused owner's monitor socket stays
    # bound but unanswered, so the peer's in-flight checkpoint
    # fetches drop to produced-nothing misses and its sync verdict
    # goes degraded — while its own scan clock, and the journaled
    # transitions it produces, run ahead of the frozen line. The
    # hold is bounded far under the armed failover budget; the watch
    # ends once the degraded verdict and one journaled window
    # transition have both landed.
    rows = []
    try:
        ctx['pause_controller'](owner)
    except Exception as exc:
        record['pause_error'] = str(exc)[:300]
        return record, evidence
    try:
        deadline = time.monotonic() + ORDER_HOLD
        while time.monotonic() < deadline:
            row = _order_row(ctx, peer)
            if row is not None:
                rows.append(row)
                if row.get('role') != 'standby':
                    record['failover'] = row
                    break
                if row.get('sync') == 'degraded' \
                        and record.get('degraded') is None:
                    record['degraded'] = row
            gained = _gained_entries(journal_path, floor)
            if record.get('degraded') is not None and gained:
                break
            time.sleep(ORDER_POLL)
    finally:
        try:
            ctx['unpause_controller'](owner)
            record['unpaused'] = True
        except Exception as exc:
            record['unpaused'] = False
            record['unpause_error'] = str(exc)[:200]
    record['window'] = rows

    # The realign on resumed pulls: the covering checkpoint applies
    # at the run's own tick and the settle it carries journals as the
    # peer's adopted command_settled. The watch keeps polling both
    # monitors between durable reads — the owner's scan is what
    # applies the due receipt, the peer's is what adopts it.
    record['realigned'] = wait_for(
        lambda: _tracking_standby(ctx, peer),
        time.monotonic() + ORDER_SETTLE, interval=ORDER_POLL)

    def adopted_ready():
        _try_role(ctx, base)
        _try_role(ctx, peer_base)
        return _adopted_settles(
            _gained_entries(journal_path, floor), command,
            ORDER_ACTOR) or None

    wait_for(adopted_ready,
             time.monotonic() + ORDER_ADOPT, interval=ORDER_POLL)
    gained = _gained_entries(journal_path, floor)
    adopted = _adopted_settles(gained, command, ORDER_ACTOR)
    record['adopted'] = [
        {'seq': entry.get('seq'), 'tick': entry.get('tick'),
         'apply_tick': _apply_tick(entry)}
        for entry in adopted]
    adopted_seqs = {entry.get('seq') for entry in adopted}
    record['window_marks'] = min(
        (pos for pos, entry in enumerate(gained)
         if entry.get('seq') in adopted_seqs), default=None)
    evidence['gained'] = [
        {'seq': entry.get('seq'), 'tick': entry.get('tick'),
         'kind': next(iter(entry.get('event') or {}), 'none')}
        for entry in gained[:40]]

    # The owner-side half of the admission: the receipt must have
    # settled applied — the outcome the peer's adopted record
    # echoes.
    receipt = None
    if index is not None:
        deadline = time.monotonic() + ORDER_SETTLE
        while receipt is None and time.monotonic() < deadline:
            receipt = _submitted_receipt(ctx, base, index, command)
            if receipt is None:
                time.sleep(ORDER_POLL)
    record['settled'] = {'outcome': _outcome_key(receipt),
                         'apply_tick': (((receipt or {}).get('outcome')
                                         or {}).get('applied') or {})
                         .get('tick')} if receipt else None

    # The axis audit: the peer's durable journal's entry ticks across
    # the run — every stamp at or after its predecessor.
    try:
        record['axis'] = _axis_audit(journal_path)
    except Exception as exc:
        record['journal_error'] = str(exc)[:300]

    record['restored'] = wait_for(
        lambda: (_pair_active(ctx) == owner or None)
        and _tracking_standby(ctx, peer),
        time.monotonic() + ORDER_SETTLE, interval=ORDER_POLL)
    if not record['restored']:
        # The next pass stages from the launch layout — walk the
        # pair back best-effort; the judge already named the
        # unrestored state above.
        _restore_launch(ctx, owner, peer)
    return record, evidence


def scenario_tracker_realign_tick_order(ctx):
    """Exercise the tracking-realign journal tick-order contract on
    the deployed pair: with the pair settled and tracking, freeze the
    field owner's container so the standby's checkpoint pulls produce
    nothing — the degraded window whose journaled stale transitions
    hold the run's tick axis ahead of the frozen line — while a
    receipted write settles on the owner; thaw the source so the
    covering checkpoint realigns the peer and lands the settled
    receipt for adoption; then audit the peer's durable journal —
    within the run, no entry may stamp a tick below an earlier
    entry's — while the peer reconverges and the launch roles
    restore."""
    case = Case(
        'tracker-realign-tick-order',
        'Tracking realign keeps the durable journal tick-ordered',
        'with the deployed pair settled and tracking, each pass '
        'freezes the field owner so the tracking standby\'s '
        'checkpoint pulls produce nothing and its sync goes degraded '
        'while its own scan clock — and its journaled stale '
        'transitions — run ahead of the frozen line; a receipted '
        'write settles on the owner so the covering checkpoint on '
        'resumed pulls carries it for adoption; the peer\'s durable '
        'journal must then show every entry stamped at or after its '
        'predecessor\'s tick within the run — no journaled record '
        'rewinds the axis — while the peer reconverges and the '
        'pair\'s launch roles restore; two passes produce identical '
        'digests')
    try:
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries only one endpoint — the pair '
                               'the tick-order leg needs is absent')
        for action in ('pause_controller', 'unpause_controller'):
            if ctx.get(action) is None:
                return case.finish('inconclusive', 'the run context '
                                   'carries no ' + action + ' action '
                                   '— the frozen-source partition '
                                   'cannot be driven')
        journals = ctx.get('journal_files') or {}
        if not all(journals.get(name)
                   and Path(journals[name]).is_file()
                   for name in ('active', 'standby')):
            return case.finish('inconclusive', 'the run context '
                               'carries no per-controller journal '
                               'files — the durable tick-axis audit '
                               'cannot run')
        for name in ('active', 'standby'):
            try:
                _role(ctx, ctx[name])
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s '
                                   'monitor is unreachable: '
                                   + str(exc)[:200])

        # The launch layout the passes stage from: the unconfigured
        # peer holds the field and the launched standby tracks it —
        # the only posture whose frozen source drives the peer's
        # degraded window. A swapped layout is walked back before the
        # leg reports.
        if _pair_active(ctx) != 'active':
            _restore_launch(ctx, 'active', 'standby')
        deadline = time.monotonic() + ORDER_SETTLE
        if wait_for(lambda: _pair_active(ctx) == 'active'
                    and 'active' or None, deadline,
                    interval=ORDER_POLL) != 'active':
            reports = {name: _try_role(ctx, ctx[name])
                       for name in ('active', 'standby')}
            if all(report is None for report in reports.values()):
                return case.finish('inconclusive', 'the pair is '
                                   'unreachable — monitor endpoints '
                                   + ctx['active'] + ' and '
                                   + ctx['standby'])
            return case.finish('inconclusive', 'the pair never '
                               'settled on its launch layout — the '
                               'unconfigured peer must hold the '
                               'field for the frozen-source window '
                               'the leg induces')
        if wait_for(lambda: _tracking_standby(ctx, 'standby'),
                    deadline, interval=ORDER_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the realign the '
                               'leg stages has no tracking peer')

        # The contract surface: the tracking peer's durable journal
        # must carry `entry` records on an integer tick axis — the
        # attributed durable record the #830 clause audits; a run
        # whose journal has nothing stamped on the axis predates the
        # surface and cannot be judged.
        try:
            axis = _axis_audit(ctx['journal_files']['standby'])
        except Exception as exc:
            return case.finish('inconclusive', 'the standby\'s '
                               'durable journal cannot be read: '
                               + str(exc)[:200])
        if not axis['entries']:
            return case.finish('inconclusive', 'the standby\'s '
                               'durable journal carries no tick-axis '
                               'records — the staged run predates '
                               'the attributed durable-record '
                               'contract the leg audits')
        owner, peer = 'active', 'standby'
        case.observe('field owner: ' + owner + ' (' + ctx[owner]
                     + '); tracking peer under audit: ' + peer)

        digests = []
        try:
            for number in (1, 2):
                violations = {}

                def note(key, diagnostic, detail):
                    violations.setdefault(key, (diagnostic, detail))

                record, evidence = _tick_order_pass(
                    ctx, number, owner, peer)
                _judge_tick_order(record, note)
                digest = _tick_order_digest(violations)
                evidence['record'] = record
                evidence['digest'] = dict(digest)
                evidence['violations'] = {
                    key: diagnostic for key, (diagnostic, _)
                    in violations.items()}
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'realign-tick-order-pass-' + str(number)
                    + '.json', evidence)
                case.evidence('file', ref, 'tick-order pass '
                              + str(number) + ' — the induction '
                              'admission, the degraded-window watch '
                              'rows, the adopted settle, the durable '
                              'tick-axis audit, the restore, and the '
                              'normalized digest')
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
        finally:
            # The launch layout for the cases behind this one — a
            # clean pass restores it by construction; an aborted pass
            # gets the source thawed and the documented role order
            # run again, best-effort.
            _restore_launch(ctx, owner, peer)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', DIAG_NONDET + ': the two passes\' digests '
                'diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two frozen-source passes, identical digests: '
                     + json.dumps(digests[0], sort_keys=True))

        # The unchecked-diagnostic self-check: the tick-order judge
        # replays each planted negative it must name; a silent judge
        # means the leg can no longer catch what it names.
        slipped = _tick_order_self_check()
        if slipped:
            return case.finish('failed', DIAG_UNCHECKED
                               + ': planted negatives slipped the '
                               'leg’s own audits: '
                               + ', '.join(slipped))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
