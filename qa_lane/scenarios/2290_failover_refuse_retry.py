"""The failover_refuse_retry leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the leg extends the failover cluster behind the
# refusal-journal leg — it stages the same armed gate's refused
# fire, one refusal cause earlier than the voided-proof window that
# leg audits, and it restores the pair's launch layout for the
# negotiation cases behind it.
RUNS_AFTER = frozenset({'scenario_failover_refusal_journal'})


# --------------------------------------------------------------------
# The refused-fire failover-gate retry contract — the per-revision
# lane evidence for the fix #1165 lands under WW-LCM-001's
# availability clause: armed failover exists to survive active loss,
# so a transient or correctly-refused self-promotion at the
# budget-th miss must not disarm the gate for the rest of the
# dead-source episode. The defect had failover_due() exact-equality
# plus the converged proof voided at misses>budget, so one refused
# fire — correct at fire time against a live incumbent — permanently
# disarmed failover even after the incumbent genuinely died: the
# armed peer stranded on standby forever while the field stood
# dead-owned.
#
# The staging reproduces the refused fire on the launched pair
# directly: a foreign attachment's controller-marked unconditional
# claim_writer preempts the field owner unconditionally — live,
# unyielded, and declaring no monitor, so the fenced owner's
# in-place demotion turns the armed standby's tracked line
# ownerless while no orphan-resolution probe can ever dial the
# incumbent. From there the armed peer's pulls land orphaned
# checkpoints: every apply counts the cycle's miss AND re-proves
# convergence — Orphaned is a promotable verdict — so the miss
# accounting climbs to the armed budget with the proof standing,
# the budget-th fire runs the conditional orphan claim, and the
# live incumbent's unyielded claim refuses it field_claim_failed.
# The contract under audit: the gate stays armed across the
# refusal — misses count past the budget beside the still-standing
# proof, the episode journals exactly one promotion_refused naming
# the live-incumbent cause and the fired count — and when the
# incumbent's attachment drops, leaving its claim dead-owned and
# the plant frozen, the next due cycle's retry preempts the dead
# token and the peer promotes automatically, journaled
# standby->promoting->active origin failover with no actor.
#
# The audits read the armed peer's served /role (the decision-100
# standing proof — misses k of N beside converged through the
# refusal and past it), its durable --journal-file and served
# /journal tail (the one named refusal, then the failover-origin
# role walk), and the pair's restored launch roles. Named
# diagnostics failover-retry-failed for a contract miss — a gate
# that preempted the live incumbent, a proof that voided inside
# the armed window, a silent or flooded refusal trail, a peer
# stranded on standby after the incumbent died, an operator-shaped
# promotion record — and failover-retry-nondeterministic when the
# passes disagree or the rig answers with instability: a refused
# incumbent claim, an island that never forms, a starved watch,
# misses that never reach the budget, a dropped served-journal
# read, a field that moved under the held claim.
# failover-retry-unchecked reports the self-check's planted
# negatives slipping the leg's own audits; inconclusive when the
# staged run predates the contract or the seams the leg needs are
# absent.

RETRY_SETTLE = 45    # bound on each switch/restore settle
RETRY_WINDOW = 60    # bound on the orphaned miss run reaching the
                     # declared budget — ~12 s of counted misses at
                     # the 100 ms cadence plus pull-cycle slack
RETRY_HOLD = 4       # the post-refusal watch — misses keep counting
                     # past the fired boundary beside the standing
                     # proof, so a disarmed gate has scans to show
                     # itself in
RETRY_POLL = 0.2     # the window watch cadence
DIAG_FAILED = 'failover-retry-failed'
DIAG_NONDET = 'failover-retry-nondeterministic'
DIAG_UNCHECKED = 'failover-retry-unchecked'
# The incumbent attachment's foreign owner token — never a
# controller's pinned token: the live unyielded claim whose hold
# fences the launched owner and whose refusal the budget-th fire
# must answer, then the dead-owned claim its drop leaves for the
# retry to preempt.
RETRY_FOREIGN = 0x7161_2d72_6574_7279  # "qa-retry"


def _retry_row(ctx, name):
    """One normalized watch row off the armed peer's served /role —
    the reported role, the sync verdict, and the failover evidence
    the armed gate's window climbs on: the standing proof (converged)
    beside the miss accounting (misses of the armed budget). None
    when the peer does not answer — a dropped observation, never a
    row."""
    report = _try_role(ctx, ctx[name])
    if report is None:
        return None
    failover = report.get('failover')
    if not isinstance(failover, dict):
        failover = {}
    sync = report.get('sync')
    verdict = next(iter(sync), None) if isinstance(sync, dict) else sync
    return {'role': report.get('role'), 'sync': verdict,
            'converged': failover.get('converged'),
            'misses': failover.get('misses'),
            'budget': failover.get('budget')}


def _retry_orphaned(ctx, name):
    """The watch row while the peer reports the ownerless-line
    verdict — the island member's state — else None."""
    row = _retry_row(ctx, name)
    return row if (row or {}).get('sync') == 'orphaned' else None


def _retry_refusals(ctx, name, floor):
    """The (seq, body) promotion_refused rows the peer's durable
    --journal-file carries since `floor` records — the audit surface
    the refused fire owes its entry to."""
    out = []
    for item in _journal_entries(ctx['journal_files'][name])[floor:]:
        entry = item.get('entry') or {}
        event = entry.get('event') or {}
        if isinstance(event.get('promotion_refused'), dict):
            out.append({'seq': entry.get('seq'),
                        'body': event['promotion_refused']})
    return out


def _retry_served_refusals(ctx, name, floor):
    """The promotion_refused bodies the peer's served /journal tail
    carries since `floor` seq — None when the read itself dropped."""
    try:
        _, journal = http_json('GET', ctx[name] + '/journal?since='
                               + str(floor))
    except Exception:
        return None
    out = []
    for entry in _journal_list(journal):
        event = entry.get('event') or {}
        if isinstance(event.get('promotion_refused'), dict):
            out.append(event['promotion_refused'])
    return out


def _retry_walk(ctx, name, floor):
    """The role_changed transitions toward field ownership the
    peer's durable journal carried since `floor` records — (seq,
    from, to, origin, actor-flag) rows: the retry's own trail must
    read standby->promoting->active origin failover with no actor —
    an operator-attributed walk is a requested switch, not the armed
    gate's."""
    out = []
    for item in _journal_entries(ctx['journal_files'][name])[floor:]:
        entry = item.get('entry') or {}
        change = (entry.get('event') or {}).get('role_changed')
        if isinstance(change, dict) \
                and change.get('to') in ('promoting', 'active'):
            out.append({'seq': entry.get('seq'),
                        'from': change.get('from'),
                        'to': change.get('to'),
                        'origin': change.get('origin'),
                        'actor': 'actor' in change})
    return out


def _retry_active(ctx, name):
    """The peer's served /role report while it reports field
    ownership — the retry's landing row — else None."""
    report = _try_role(ctx, ctx[name])
    return report if (report or {}).get('role') == 'active' else None


def _retry_field_tick(ctx):
    """The simulated plant's current step tick — the frozen-field
    probe: the incumbent never writes and the fenced owner cannot,
    so a held claim means a held tick."""
    return (_try_plant(ctx, {'op': 'ping'}) or {}).get('tick')


def _judge_retry(record, note):
    """Audit one pass's record — replayable, so the self-check can
    hand it planted negatives. `note(key, diagnostic, detail)`
    records each clause the record violates: DIAG_FAILED tags the
    retry contract — the live incumbent the conditional claim must
    refuse, the standing proof the armed window serves, the one
    named promotion_refused on both journal surfaces, the automatic
    re-fire that must land the peer on the field after the incumbent
    dies, the failover-origin walk, the restored launch roles — and
    DIAG_NONDET tags the instability the contract does not answer
    for: a refused incumbent claim, an island that never forms, a
    starved watch, misses that never reach the budget, a dropped
    served-journal read, a field that moved under the held claim.
    An aborted stage ends the audit where the pass ended — the later
    keys it never wrote are not clauses."""
    def failed(key, detail):
        note(key, DIAG_FAILED, detail)

    def nondet(key, detail):
        note(key, DIAG_NONDET, detail)

    budget = record.get('budget')
    if not isinstance(budget, int) or isinstance(budget, bool) \
            or budget <= 0:
        nondet('armed', 'the armed peer\'s first read carried no '
               'declared budget — the failover evidence the leg '
               'audits was never observed: '
               + json.dumps(budget))
        return
    claim = record.get('claim') or {}
    if claim.get('result') != 'done':
        nondet('claim', 'the incumbent\'s claim_writer answered '
               + json.dumps(claim)[:200] + ' — the live unyielded '
               'claim the refused fire needs never stood')
        return
    if not record.get('orphaned'):
        nondet('island', 'the armed peer never reported the '
               'orphaned verdict — the fenced owner\'s in-place '
               'demotion never turned its tracked line ownerless')
        return
    rows = record.get('window') or []
    if not rows:
        nondet('watch', 'the refused-window watch collected no '
               'served rows — the audit had nothing to read')
        return
    moved = next((row for row in rows
                  if row.get('role') != 'standby'), None)
    if moved is not None:
        failed('preempted', 'the armed peer left standby while the '
               'live incumbent held the field — the conditional '
               'claim preempted the owner it must refuse: '
               + json.dumps(moved)[:200])
    voided = next((row for row in rows
                   if row.get('converged') is not True), None)
    if voided is not None:
        failed('proof-voided', 'the served proof read voided '
               'inside the orphaned window — every orphaned apply '
               'must re-prove convergence, so a voided row here is '
               'the disarm the fix exists to prevent: '
               + json.dumps(voided)[:200])
    offverdict = next((row for row in rows
                       if row.get('sync') != 'orphaned'), None)
    if offverdict is not None:
        nondet('verdict', 'the window served a non-orphaned '
               'verdict ' + json.dumps(offverdict)[:200]
               + ' — the ownerless line did not hold for the climb')
    peak = max((row.get('misses') for row in rows
                if isinstance(row.get('misses'), int)
                and not isinstance(row.get('misses'), bool)),
               default=None)
    if peak is None or peak < budget:
        nondet('boundary', 'the armed peer\'s misses peaked at '
               + json.dumps(peak) + ' below the declared budget '
               + str(budget) + ' — the gate never fired inside the '
               'window')
        return
    durable = record.get('durable_refusals')
    if durable is None:
        return
    refusal_seq = None
    if len(durable) != 1:
        failed('refusal-row', 'the durable journal holds '
               + json.dumps(len(durable)) + ' promotion_refused '
               'entries for the refused window instead of exactly '
               'one — a refused fire that journals nothing reads '
               'identical to a peer that never armed, and a '
               're-journaling retry floods the trail')
    else:
        refusal = durable[0]
        refusal_seq = refusal.get('seq')
        body = refusal.get('body') or {}
        error = body.get('error')
        if not isinstance(error, dict) \
                or 'field_claim_failed' not in error:
            failed('refusal-row', 'the journaled refusal names '
                   + json.dumps(error)[:200] + ' — the live '
                   'incumbent\'s unyielded claim is the cause the '
                   'refused fire must name')
        misses = body.get('misses')
        if not isinstance(misses, int) or isinstance(misses, bool) \
                or misses < budget:
            failed('refusal-row', 'the journaled refusal fired at '
                   'misses=' + json.dumps(misses) + ' below the '
                   'declared budget ' + str(budget) + ' — the row '
                   'does not name the boundary it fired at')
    served = record.get('served_refusals')
    if served is None:
        nondet('served', 'the served-journal read dropped after '
               'the refused window — the tail audit never landed')
    elif served != [row.get('body') for row in durable]:
        failed('served', 'the served tail holds '
               + json.dumps(served)[:200] + ' where the durable '
               'file holds '
               + json.dumps([row.get('body') for row in
                             durable])[:200] + ' — the two audit '
               'surfaces disagree')
    frozen = record.get('frozen') or {}
    if isinstance(frozen.get('before'), int) \
            and isinstance(frozen.get('after'), int) \
            and frozen['after'] != frozen['before']:
        nondet('field-advanced', 'the field advanced under the '
               'held foreign claim — a write the incumbent never '
               'sent landed: ' + json.dumps(frozen))
    promoted = record.get('promoted')
    if promoted is None:
        failed('stranded', 'the armed peer never promoted after '
               'the incumbent died — one refused fire disarmed the '
               'gate for the rest of the dead-source episode, the '
               'defect the contract exists to close')
    walk = record.get('walk') or []
    if promoted is not None or walk:
        pairs = [(row.get('from'), row.get('to')) for row in walk]
        if pairs != [('standby', 'promoting'), ('promoting', 'active')]:
            failed('record', 'the retry\'s durable walk is '
                   + json.dumps(pairs) + ' — the re-fired gate '
                   'owes standby->promoting->active journaled '
                   'after the refused attempt')
        else:
            if isinstance(refusal_seq, int) \
                    and isinstance(walk[0].get('seq'), int) \
                    and walk[0]['seq'] < refusal_seq:
                failed('record', 'the promotion journaled ahead '
                       'of the refused fire — the walk cannot be '
                       'the gate\'s retry')
            for row in walk:
                if row.get('origin') != 'failover':
                    failed('record', 'the ' + str(row.get('from'))
                           + '->' + str(row.get('to'))
                           + ' transition journaled origin '
                           + json.dumps(row.get('origin'))
                           + ' — the re-fired gate\'s switch must '
                           'record failover, not a bare-asserted '
                           'request')
                if row.get('actor'):
                    failed('record', 'the ' + str(row.get('from'))
                           + '->' + str(row.get('to'))
                           + ' transition carries an operator '
                           'actor — the automatic retry records '
                           'no requester')
    if not record.get('restored'):
        failed('restored', 'the pair never settled back to its '
               'launch layout — the launched owner active, the '
               'armed peer tracking behind it')


def _retry_digest(violations):
    """The pass's normalized verdict record — identical across clean
    passes; each field is the clean value only while no violation —
    contract or instability — names its clause."""
    def clean(*keys):
        return not any(key in violations for key in keys)
    return {
        'armed': 'declared' if clean('armed') else 'absent',
        'incumbent': 'claimed' if clean('claim') else 'refused',
        'island': 'orphaned' if clean('island', 'verdict')
            else 'unformed',
        'window': 'boundary' if clean('watch', 'boundary')
            else 'unreached',
        'proof': 'standing'
            if clean('proof-voided', 'preempted') else 'breached',
        'row': 'one-named'
            if clean('refusal-row', 'served') else 'absent',
        'retry': 'promoted' if clean('stranded', 'record')
            else 'stranded',
        'roles': 'restored' if clean('restored') else 'unrestored'}


def _retry_self_check():
    """The leg's unchecked-diagnostic self-test: replay the retry
    judge over each planted negative the issue names — the refused
    fire journaling nothing, a flooded trail, an unnamed cause, a
    row that under-reports the fired count, the conditional claim
    preempting the live incumbent, the proof voided inside the
    armed window, the doctored gate that stays disarmed after the
    refusal and never re-fires, an operator-attributed promotion
    walk — and require the judge to note each, with the instability
    cases reporting nondeterministic rather than failed. A silent
    judge returns the negative names it let through."""
    slipped = []

    def clean_record():
        def rows():
            return [{'role': 'standby', 'sync': 'orphaned',
                     'converged': True, 'misses': m, 'budget': 120}
                    for m in (60, 90, 119, 120, 124, 130)]
        refusal = {'seq': 41,
                   'body': {'error': {'field_claim_failed': {
                       'detail': 'writer claim held by 7f6a1d'}},
                       'misses': 120}}
        walk = [{'seq': 42, 'from': 'standby', 'to': 'promoting',
                 'origin': 'failover', 'actor': False},
                {'seq': 43, 'from': 'promoting', 'to': 'active',
                 'origin': 'failover', 'actor': False}]
        return {'budget': 120,
                'claim': {'result': 'done'},
                'orphaned': {'role': 'standby', 'sync': 'orphaned',
                             'converged': True, 'misses': 1,
                             'budget': 120},
                'window': rows(),
                'durable_refusals': [refusal],
                'served_refusals': [dict(refusal['body'])],
                'frozen': {'before': 300, 'after': 300},
                'promoted': {'role': 'active', 'tick': 500},
                'walk': walk,
                'restored': True}

    def expect(name, mutate, diagnostic=DIAG_FAILED):
        record = clean_record()
        mutate(record)
        found = {}
        _judge_retry(
            record,
            lambda key, diag, detail: found.setdefault(key, diag))
        if diagnostic not in found.values():
            slipped.append(name)

    # The fired-but-refused gate left the durable trail silent —
    # indistinguishable from a peer that never armed.
    expect('refusal-silent', lambda record:
           record.update({'durable_refusals': []}))
    # ... and the same silence on the served tail alone.
    expect('served-silent', lambda record:
           record.update({'served_refusals': []}))
    # A retry that journals once per scan floods the trail.
    expect('refusal-flooded', lambda record:
           record.update({'durable_refusals':
                          record['durable_refusals'] * 3}))
    # The row that names a cause the live-incumbent window never
    # produced names nothing an auditor can use.
    expect('refusal-unnamed', lambda record:
           record['durable_refusals'][0]['body'].update(
               {'error': {'not_converged': {
                   'sync': {'degraded': {
                       'detail': 'pull refused'}}}}}))
    # A row fired below the declared budget mis-attributes the
    # boundary it names.
    expect('refusal-under-counted', lambda record:
           record['durable_refusals'][0]['body'].update(
               {'misses': 80}))
    # The issue's doctored negative: the gate asserted as
    # re-firing while it stays disarmed after the refusal — the
    # stranded peer is the defect's own shape.
    expect('gate-disarmed', lambda record:
           record.update({'promoted': None, 'walk': []}))
    # The conditional claim preempting the live incumbent is the
    # stale-island breach the orphan claim exists to refuse.
    expect('gate-preempted', lambda record:
           record['window'][2].update({'role': 'promoting'}))
    # A proof that reads voided inside the armed window is the
    # disarm mechanism the fix removed.
    expect('proof-voided', lambda record:
           record['window'][4].update({'converged': False}))
    # An operator-attributed promotion is a requested switch, not
    # the armed gate's automatic retry.
    expect('operator-switch', lambda record:
           record['walk'][0].update({'origin': 'operator',
                                     'actor': True}))
    # The pair left off its launch layout strands the cases behind.
    expect('unrestored', lambda record:
           record.update({'restored': False}))
    # The instability the contract does not answer for must report
    # nondeterministic, not failed: a starved watch, misses that
    # never reach the budget, a dropped served read, a refused
    # incumbent claim, an island that never formed.
    expect('watch-starved', lambda record:
           record.update({'window': []}), DIAG_NONDET)
    expect('boundary-unreached', lambda record: [
        row.update({'misses': 60})
        for row in record['window']], DIAG_NONDET)
    expect('served-dropped', lambda record:
           record.update({'served_refusals': None}), DIAG_NONDET)
    expect('claim-refused', lambda record:
           record.update({'claim': {'result': 'error',
                                    'error': {'kind': 'fenced'}}}),
           DIAG_NONDET)
    expect('island-unformed', lambda record:
           record.update({'orphaned': None}), DIAG_NONDET)
    return slipped


def _restore_retry_layout(ctx):
    """Best-effort launch-layout restore: the launched owner back
    over the field with the armed peer tracking behind it — used by
    the layout gate and the cleanup path alike. The ordering the
    claim arbitration demands: an owning armed peer demotes first —
    its release yields the claim the restore hands back — then the
    launched owner's promote retries while it re-proves promotable,
    an orphaned posture counting as promotable once it resolves
    onto the surviving peer. Everything retries inside the bound
    and swallows refusal."""
    try:
        deadline = time.monotonic() + RETRY_SETTLE * 2
        while time.monotonic() < deadline:
            owner = _try_role(ctx, ctx['active'])
            peer = _try_role(ctx, ctx['standby'])
            peer_role = (peer or {}).get('role')
            if peer_role == 'demoting':
                pass  # mid-release — let it settle
            elif peer_role in ('active', 'promoting'):
                _settle_call(ctx['standby'] + '/demote')
            elif (owner or {}).get('role') == 'standby':
                # Refused while unconverged heals as the peer's
                # pulls re-prove a promotable posture.
                _settle_call(ctx['active'] + '/promote')
            if (owner or {}).get('role') == 'active' \
                    and peer_role == 'standby' \
                    and 'tracking' in ((peer or {}).get('sync')
                                       or {}):
                return
            time.sleep(RETRY_POLL)
    except Exception:
        pass


def _retry_pass(ctx, number):
    """One refused-fire pass: land the incumbent's controller-marked
    foreign claim — live, unyielded, declaring no monitor — so the
    fenced owner's in-place demotion leaves the armed peer orphaned
    on ownerless checkpoints; watch the miss accounting climb to
    the armed budget beside the standing proof and the refused fire
    journal its one named row; drop the incumbent so the claim
    stands dead-owned and the plant freezes; then read the armed
    peer's automatic promotion and its failover-origin walk before
    restoring the launch layout. Returns (record, evidence): the
    record is what the judge replays; an aborted stage simply
    leaves its later keys absent for the judge to name."""
    record = {}
    evidence = {'pass': number}

    row = _retry_row(ctx, 'standby')
    record['budget'] = (row or {}).get('budget')

    # The incumbent: a foreign attachment's controller-marked
    # unconditional claim — live and unyielded while the stream
    # stays open, dead-owned the moment it drops, and declaring no
    # monitor so no orphan-resolution probe can ever re-point the
    # armed peer onto it.
    stream = None
    try:
        stream = _plant_connect(ctx)
        stream.settimeout(10)
        record['claim'] = _plant_request(
            stream, {'op': 'claim_writer', 'owner': RETRY_FOREIGN,
                     'controller': True})
    except Exception as exc:
        record['claim'] = {'error': str(exc)[:200]}
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
        _restore_retry_layout(ctx)
        return record, evidence
    if (record['claim'] or {}).get('result') != 'done':
        stream.close()
        _restore_retry_layout(ctx)
        return record, evidence

    gate_floor = gate_served = None
    try:
        record['frozen'] = {'before': _retry_field_tick(ctx)}
        gate_floor = len(_journal_entries(
            ctx['journal_files']['standby']))
        gate_served = _journal_cursor(ctx, ctx['standby'])

        # The island onset: the fenced owner's in-place demotion
        # turns the armed peer's tracked line ownerless — every
        # pull from here is an orphaned apply, one counted miss and
        # one convergence re-proof per scan.
        record['orphaned'] = wait_for(
            lambda: _retry_orphaned(ctx, 'standby'),
            time.monotonic() + RETRY_SETTLE, interval=RETRY_POLL)

        if record['orphaned'] is not None:
            # The refused window: every row must read standby
            # beside the orphaned verdict and the standing proof
            # while the miss accounting climbs to the armed
            # budget — the fire — and past it through the hold
            # while the incumbent holds.
            rows = []
            deadline = time.monotonic() + RETRY_WINDOW
            while time.monotonic() < deadline:
                row = _retry_row(ctx, 'standby')
                if row is not None:
                    rows.append(row)
                    if row.get('role') != 'standby' or (
                            isinstance(row.get('misses'), int)
                            and isinstance(row.get('budget'), int)
                            and row['misses'] >= row['budget']):
                        break
                time.sleep(RETRY_POLL)
            end = time.monotonic() + RETRY_HOLD
            while time.monotonic() < end:
                row = _retry_row(ctx, 'standby')
                if row is not None:
                    rows.append(row)
                time.sleep(RETRY_POLL)
            record['window'] = rows
            record['durable_refusals'] = _retry_refusals(
                ctx, 'standby', gate_floor)
            record['served_refusals'] = _retry_served_refusals(
                ctx, 'standby', gate_served)
        record['frozen']['after'] = _retry_field_tick(ctx)
    finally:
        # Stopping the incumbent: the attachment's drop leaves its
        # claim standing dead-owned — the plant frozen — and the
        # next due cycle's retry is the fire the contract owes.
        try:
            stream.close()
        except Exception:
            pass

    # The retry: the still-armed gate's next due cycle preempts the
    # dead-owned claim and the peer promotes automatically — never
    # stranded after one refusal.
    if record.get('window') is not None:
        record['promoted'] = wait_for(
            lambda: _retry_active(ctx, 'standby'),
            time.monotonic() + RETRY_SETTLE, interval=RETRY_POLL)
        record['walk'] = _retry_walk(ctx, 'standby', gate_floor)

    _restore_retry_layout(ctx)
    record['restored'] = wait_for(
        lambda: (_pair_active(ctx) == 'active' or None)
        and _tracking_standby(ctx, 'standby'),
        time.monotonic() + RETRY_SETTLE, interval=RETRY_POLL)
    return record, evidence


def scenario_failover_refuse_retry(ctx):
    """Pin the refused-fire retry contract on the run's launched
    pair: a foreign attachment's controller-marked claim preempts
    the field owner and holds — live, unyielded, naming no monitor
    — so the fenced owner's in-place demotion leaves the armed
    standby applying ownerless checkpoints; the orphaned miss run
    reaches the armed budget beside the standing convergence
    proof, the budget-th fire's conditional claim refuses
    field_claim_failed against the live incumbent exactly once on
    both journal surfaces, and when the incumbent drops the gate
    must fire again — the peer promoting automatically, journaled
    standby->promoting->active origin failover with no actor —
    then the launch layout restores and two passes produce
    identical digests."""
    case = Case(
        'failover-refuse-retry',
        'A refused failover fire still promotes when the '
        'incumbent dies',
        'with the launched pair settled — the armed standby '
        'tracking the field owner — a foreign attachment\'s '
        'controller-marked unyielded claim preempts the field, '
        'fencing the owner into its in-place demotion so the '
        'armed peer\'s pulls land orphaned checkpoints; the '
        'served role must read standby beside the orphaned '
        'verdict, the standing proof, and misses climbing to '
        'and past the armed budget while the incumbent holds, '
        'the durable --journal-file and the served /journal '
        'tail must each hold exactly one promotion_refused '
        'naming field_claim_failed and the fired count, and '
        'once the incumbent drops — the field dead-owned and '
        'frozen — the still-armed gate fires again and the '
        'peer promotes automatically, journaled '
        'standby->promoting->active origin failover with no '
        'actor, the launch layout restores, and two passes '
        'produce identical digests')
    try:
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries only one endpoint — the '
                               'pair the retry leg needs is '
                               'absent')
        if ctx.get('plant') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no published plant '
                               'endpoint — the incumbent '
                               'attachment cannot be staged')
        journals = ctx.get('journal_files') or {}
        if not (journals.get('standby')
                and Path(journals['standby']).is_file()):
            return case.finish('inconclusive', 'the run context '
                               'carries no journal file for the '
                               'armed peer — the durable '
                               'promotion_refused audit cannot '
                               'run')
        for name in ('active', 'standby'):
            try:
                _role(ctx, ctx[name])
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s '
                                   'monitor is unreachable: '
                                   + str(exc)[:200])
        try:
            _plant_probe(ctx, {'op': 'ping'})
        except Exception as exc:
            return case.finish('inconclusive', 'the simulated '
                               'plant is unreachable: '
                               + str(exc)[:200])

        # The armed-gate subject: the launched standby is the
        # pair's only failover gate; a release serving no
        # failover evidence predates the contract this leg
        # audits.
        armed = _retry_row(ctx, 'standby')
        if not isinstance((armed or {}).get('budget'), int) \
                or isinstance((armed or {}).get('budget'), bool):
            return case.finish('inconclusive', 'the launched '
                               'standby serves no armed failover '
                               'evidence — the staged release '
                               'predates the --auto-promote '
                               'contract this leg audits')

        # The launch layout the passes stage from: the unarmed
        # owner over the field, the armed peer tracking and
        # converged — a swapped or unsettled pair is restored
        # before the leg reports.
        deadline = time.monotonic() + RETRY_SETTLE
        if _pair_active(ctx) != 'active' \
                or _tracking_standby(ctx, 'standby') is None:
            _restore_retry_layout(ctx)
        settled = wait_for(
            lambda: (_pair_active(ctx) == 'active' or None)
            and _tracking_standby(ctx, 'standby'),
            deadline, interval=RETRY_POLL)
        if settled is None:
            return case.finish('inconclusive', 'the pair never '
                               'settled on its launch layout — '
                               'the armed peer must own a '
                               'tracking, converged proof for '
                               'the refused window the leg '
                               'stages')
        case.observe('armed failover gate: standby (' + ctx['standby']
                     + '); incumbent claim on ' + str(ctx['plant'])
                     + ' under token ' + str(RETRY_FOREIGN))

        digests = []
        try:
            for number in (1, 2):
                violations = {}

                def note(key, diagnostic, detail):
                    violations.setdefault(key, (diagnostic, detail))

                record, evidence = _retry_pass(ctx, number)
                _judge_retry(record, note)
                digest = _retry_digest(violations)
                evidence['record'] = record
                evidence['digest'] = dict(digest)
                evidence['violations'] = {
                    key: diagnostic for key, (diagnostic, _)
                    in violations.items()}
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'failover-refuse-retry-pass-' + str(number)
                    + '.json', evidence)
                case.evidence('file', ref, 'failover-refuse-retry '
                              'pass ' + str(number) + ' — the '
                              'incumbent claim, the orphaned '
                              'onset, the refused-window watch '
                              'rows, the durable and served '
                              'promotion_refused bodies, the '
                              'frozen-field probe, the retry\'s '
                              'promotion and failover-origin '
                              'walk, the restore, and the '
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
            # The launch layout for the cases behind this one —
            # a clean pass restores it by construction; an
            # aborted pass gets the documented role order run
            # again, best-effort.
            _restore_retry_layout(ctx)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', DIAG_NONDET + ': the two passes\' '
                'digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two refused-fire passes, identical '
                     'digests: '
                     + json.dumps(digests[0], sort_keys=True))

        # The unchecked-diagnostic self-check: the retry judge
        # replays each planted negative it must name; a silent
        # judge means the leg can no longer catch what it names.
        slipped = _retry_self_check()
        if slipped:
            return case.finish('failed', DIAG_UNCHECKED
                               + ': planted negatives slipped the '
                               'leg’s own audits: '
                               + ', '.join(slipped))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
