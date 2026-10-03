"""The failover_refusal_journal leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the leg extends the failover-attribution cluster — the
# proof-report leg ahead of it pins the armed gate's served evidence
# contract this one's journal audit reads — and it restores the
# pair's launch layout for the negotiation cases behind it.
RUNS_AFTER = frozenset({'scenario_failover_proof_report'})


# --------------------------------------------------------------------
# The fired-but-refused gate's durable trail — the
# failover-refusal-leaves-no-durable-trace finding pinned as per-run
# lane evidence for WW-LCM-001's peer-health journal clause: the
# automatic failover gate is the pair's first automatic actor, and
# its refused attempts are the least visible thing a journal reader
# sees. A fired-and-refused self-promotion queues no role_changed of
# its own, so without an entry of its own the durable trail reads "a
# dead source, a parked standby, silence" — indistinguishable from a
# peer that never armed at all. The contract the leg audits: an
# armed standby whose convergence proof is voided and whose
# evidence-free misses reach the armed budget fires the gate and is
# refused, and the journal must carry exactly one promotion_refused
# entry — naming the refusal cause and the miss count the gate fired
# at — on both the durable --journal-file and the served /journal
# tail, beside no role transition the attempt never made.
#
# The staging reproduces the finding's voided window on the launched
# pair directly: the armed standby — the pair's only failover gate —
# is promoted over the field (the incumbent's fencing-loss demotes
# it in place — the misordered-failover switch, keyed or unkeyed),
# the launched owner's container is stopped, and the armed peer is
# demoted onto its configured — now dead — tracking source. Every
# scan from there is a produced-nothing miss with the proof already
# voided: the miss count climbs to the armed budget, the boundary
# fires the gate, and the refusal is what the journal must carry —
# once, bounded, while misses keep counting past the voided
# boundary. The audit then watches the peer's served role through
# the climb (every row must read standby — a gate that promotes on a
# voided proof is the wrong answer entirely), counts the
# promotion_refused rows on both journal surfaces, requires the row
# to name not_converged and the fired miss count, and requires the
# durable trail free of ownership transitions through the window.
#
# The refusal is only half the contract the journal owes: a gate
# whose refused attempt latched it closed would read the same
# silence as a healthy one in this window. So the pass's second
# half re-stands the proof the refusal voided — the launched
# owner's restart re-claims the released field, the peer's pulls
# re-converge it — then silences the owner again: this dead source
# is an eligible fire, the budget-th miss arriving while the
# convergence proof still stands, and the armed gate must lift the
# peer into the promotion its durable trail carries beside the one
# refusal row. A second promotion_refused where the standing proof
# makes the fire a promotion is a relapse the bounded record does
# not allow; a parked gate is the availability miss the whole
# clause exists to name. The documented demote-then-start order
# restores the launch layout for the cases behind. Named diagnostics
# failover-refusal-journal-failed for a contract miss —
# failover-refusal-journal-nondeterministic when two passes disagree
# or the rig answers with instability instead of a verdict: a
# starved watch, misses that never reach the budget, a dropped
# served-journal read, a refused staging call, a proof that never
# re-stands — and failover-refusal-journal-unchecked when the
# self-check's planted negatives slip the leg's own audits.

REFUSAL_SETTLE = 45    # bound on each switch/demote/restore settle
REFUSAL_WINDOW = 60    # bound on the voided-gate climb — the declared
                       # 120-miss budget at the 100 ms cadence (~12 s
                       # of counted misses) plus pull-cycle slack
REFUSAL_HOLD = 4       # the post-boundary watch — misses keep
                       # counting past the refused fire, so a
                       # re-firing gate or a flooded trail has scans
                       # to show itself in
REFUSAL_POLL = 0.2     # the window watch cadence
DIAG_FAILED = 'failover-refusal-journal-failed'
DIAG_NONDET = 'failover-refusal-journal-nondeterministic'
DIAG_UNCHECKED = 'failover-refusal-journal-unchecked'


def _armed_row(ctx, name):
    """One normalized watch row off the armed peer's served /role —
    the reported role and the failover evidence the voided-gate
    window climbs on. None when the peer does not answer — a dropped
    observation, never a row."""
    report = _try_role(ctx, ctx[name])
    if report is None:
        return None
    failover = report.get('failover')
    if not isinstance(failover, dict):
        failover = {}
    return {'role': report.get('role'),
            'converged': failover.get('converged'),
            'misses': failover.get('misses'),
            'budget': failover.get('budget')}


def _eligible_row(ctx):
    """The armed peer's watch row while its convergence proof
    stands — the promotable posture the eligible-fire window is
    staged on. None while the peer has not re-stood it."""
    row = _armed_row(ctx, 'standby')
    if row is None or row.get('role') != 'standby' \
            or row.get('converged') is not True:
        return None
    return row


def _refused_rows(ctx, name, floor):
    """The promotion_refused event bodies the peer's durable
    --journal-file carries since `floor` records — the audit surface
    the fired-but-refused gate owes its entry to."""
    out = []
    for item in _journal_entries(ctx['journal_files'][name])[floor:]:
        event = (item.get('entry') or {}).get('event') or {}
        if isinstance(event.get('promotion_refused'), dict):
            out.append(event['promotion_refused'])
    return out


def _served_refusals(ctx, name, floor):
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


def _role_walks(ctx, name, floor):
    """The durable role_changed transitions toward field ownership
    the peer journaled since `floor` records — the refused attempt's
    own trail must be empty: a self-promotion the gate refuses queues
    no transition, so a promotion row inside the voided window is a
    move the attempt never legitimately made."""
    out = []
    for item in _journal_entries(ctx['journal_files'][name])[floor:]:
        event = (item.get('entry') or {}).get('event') or {}
        change = event.get('role_changed')
        if isinstance(change, dict) \
                and change.get('to') in ('promoting', 'active'):
            out.append(change)
    return out


def _judge_window(record, note):
    """Audit one pass's record — replayable, so the self-check can
    hand it planted negatives. `note(key, diagnostic, detail)`
    records each clause the record violates: DIAG_FAILED tags the
    durable-trace contract clauses — the voided gate that must stay
    parked, the one promotion_refused entry the window owes on both
    surfaces, the named cause and fired count it carries, the absent
    role transition, the armed-and-eligible fire that must still
    promote — journaled, on the named boundary, beside no second
    refusal row — the restored launch roles — and DIAG_NONDET
    tags the instability the contract does not answer for: a starved
    watch, misses that never reach the declared budget, a dropped
    served-journal read, a refused staging call, a proof that never
    re-stands. An aborted stage
    ends the audit where the pass ended — the later keys it never
    wrote are not clauses."""
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
    promote = record.get('promote') or {}
    if promote.get('status') != 200 or not record.get('owns'):
        nondet('promote', 'the arming POST /promote answered '
               + str(promote.get('status')) + ' '
               + json.dumps(promote.get('body'))[:160] + ' — the '
               'staging never made the armed peer the field owner')
        return
    demote = record.get('demote') or {}
    if demote.get('status') != 200:
        nondet('demote', 'the voiding POST /demote answered '
               + str(demote.get('status')) + ' '
               + json.dumps(demote.get('body'))[:160] + ' — the '
               'dead-source window never opened')
        return
    if not record.get('settled'):
        nondet('demote', 'the demoted peer never reported standby '
               'behind its dead source — the voided window never '
               'settled')
        return
    rows = record.get('window') or []
    if not rows:
        nondet('watch', 'the voided-gate watch collected no served '
               'rows — the audit had nothing to read')
        return
    moved = next((row for row in rows
                  if row.get('role') != 'standby'), None)
    if moved is not None:
        failed('promoted', 'the voided gate promoted anyway — the '
               'peer left standby on a proof-free window: '
               + json.dumps(moved)[:200])
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
    if len(durable) != 1:
        failed('refusal-row', 'the durable journal holds '
               + json.dumps(len(durable)) + ' promotion_refused '
               'entries for the voided window instead of exactly '
               'one — a fired-but-refused gate that journals '
               'nothing reads identical to a peer that never '
               'armed, and a refiring gate floods the trail')
    else:
        body = durable[0]
        error = body.get('error')
        if not isinstance(error, dict) or 'not_converged' not in error:
            failed('refusal-row', 'the journaled refusal names '
                   + json.dumps(error)[:200] + ' — the voided '
                   'window\'s cause is the convergence proof the '
                   'gate found lapsed')
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
               'the voided window — the tail audit never landed')
    elif served != durable:
        failed('served', 'the served tail holds '
               + json.dumps(served)[:200] + ' where the durable '
               'file holds ' + json.dumps(durable)[:200] + ' — '
               'the two audit surfaces disagree')
    if record.get('role_walks'):
        failed('role-walk', 'the voided window journaled '
               + json.dumps(record['role_walks'])[:200] + ' — a '
               'refused self-promotion queues no role transition, '
               'so a promotion row here is a move the gate never '
               'legitimately made')
    if 'reconverged' in record:
        if not record['reconverged']:
            nondet('reconverged', 'the restarted owner never '
                   're-stood the armed peer\'s convergence proof '
                   '— the eligible-fire window the leg audits '
                   'second never opened')
        else:
            fire_rows = record.get('fire_window')
            if not fire_rows:
                nondet('fire-watch', 'the eligible-fire watch '
                       'collected no served rows — the audit '
                       'had nothing to read')
            else:
                fired = next(
                    (row for row in fire_rows
                     if row.get('role') != 'standby'), None)
                peak = max(
                    (row.get('misses') for row in fire_rows
                     if isinstance(row.get('misses'), int)
                     and not isinstance(row.get('misses'), bool)),
                    default=None)
                if fired is None \
                        and (peak is None or peak < budget):
                    nondet('fire-boundary', 'the eligible '
                           'window\'s misses peaked at '
                           + json.dumps(peak) + ' below the '
                           'declared budget ' + str(budget)
                           + ' — the second fire never came due')
                else:
                    fmisses = (fired or {}).get('misses')
                    if isinstance(fmisses, int) \
                            and not isinstance(fmisses, bool) \
                            and fmisses < budget:
                        failed('fire-early', 'the eligible gate '
                               'fired at misses=' + str(fmisses)
                               + ' below the declared budget '
                               + str(budget) + ' — the boundary '
                               'the refusal row named is not the '
                               'one the promotion fired on')
                    if not record.get('fire_promoted'):
                        failed('fire-parked', 'the armed peer\'s '
                               'misses reached the budget with '
                               'the convergence proof standing '
                               'and the gate never lifted — the '
                               'later armed-and-eligible fire '
                               'the refused attempt must not '
                               'preclude never promoted: '
                               + json.dumps(fire_rows[-1])[:200])
                    elif 'fire_walks' in record \
                            and not record['fire_walks']:
                        failed('fire-unwalked', 'the eligible '
                               'promotion journaled no ownership '
                               'transition — the durable trail '
                               'that names the refusal owes the '
                               'later takeover its row')
            final = record.get('final_refusals')
            if final is not None and len(final) != 1:
                failed('refusal-relapse', 'the durable trail '
                       'holds ' + json.dumps(len(final))
                       + ' promotion_refused entries after the '
                       'eligible window — the bounded record the '
                       'refused fire owes grew a second row '
                       'where the standing proof makes the fire '
                       'a promotion')
    if not record.get('restored'):
        failed('restored', 'the pair never settled back to its '
               'launch layout — the launched owner active, the '
               'armed peer tracking behind it')


def _refusal_digest(violations):
    """The pass's normalized verdict record — identical across clean
    passes; each field is the clean value only while no violation —
    contract or instability — names its clause."""
    def clean(*keys):
        return not any(key in violations for key in keys)
    return {
        'armed': 'declared' if clean('armed') else 'absent',
        'switch': 'armed-owner' if clean('promote') else 'blocked',
        'void': 'dead-source' if clean('demote') else 'staged-out',
        'window': 'boundary' if clean('watch', 'boundary')
            else 'unreached',
        'gate': 'parked' if clean('promoted', 'role-walk')
            else 'moved',
        'row': 'one-named'
            if clean('refusal-row', 'served', 'refusal-relapse')
            else 'absent',
        'proof': 're-stood' if clean('reconverged') else 'lapsed',
        'fire': 'promoted'
            if clean('fire-watch', 'fire-boundary', 'fire-early',
                     'fire-parked', 'fire-unwalked')
            else 'unproven',
        'roles': 'restored' if clean('restored') else 'unrestored'}


def _refusal_self_check():
    """The leg's unchecked-diagnostic self-test: replay the refusal
    judge over each planted negative the issue names — the
    fired-but-refused gate journaling nothing (the finding's own
    shape), a flooded trail, an unnamed cause, a row that
    under-reports the fired count, a gate that promoted on the
    voided proof, a transition journaled beside the refusal — and
    require the judge to note each, with the instability cases
    reporting nondeterministic rather than failed. A silent judge
    returns the negative names it let through."""
    slipped = []

    def clean_record():
        def rows():
            return [{'role': 'standby', 'converged': False,
                     'misses': m, 'budget': 120}
                    for m in (60, 90, 119, 120, 124, 130)]
        def fire_rows():
            rows = [{'role': 'standby', 'converged': True,
                     'misses': m, 'budget': 120}
                    for m in (60, 90, 119)]
            rows.append({'role': 'promoting', 'converged': True,
                         'misses': 120, 'budget': 120})
            return rows
        refusal = {'error': {'not_converged': {
                       'sync': {'degraded': {
                           'detail': 'pull refused'}}}},
                   'misses': 120}
        return {'budget': 120,
                'promote': {'status': 200,
                            'body': {'role': 'promoting'}},
                'owns': True,
                'demote': {'status': 200,
                           'body': {'role': 'demoting'}},
                'settled': True,
                'window': rows(),
                'durable_refusals': [refusal],
                'served_refusals': [dict(refusal)],
                'role_walks': [],
                'reconverged': True,
                'fire_window': fire_rows(),
                'fire_promoted': True,
                'fire_walks': [
                    {'from': 'standby', 'to': 'promoting'},
                    {'from': 'promoting', 'to': 'active'}],
                'final_refusals': [dict(refusal)],
                'restored': True}

    def expect(name, mutate, diagnostic=DIAG_FAILED):
        record = clean_record()
        mutate(record)
        found = {}
        _judge_window(
            record,
            lambda key, diag, detail: found.setdefault(key, diag))
        if diagnostic not in found.values():
            slipped.append(name)

    # The finding's own shape: the fired-but-refused gate left the
    # durable trail silent — indistinguishable from never armed.
    expect('refusal-silent', lambda record:
           record.update({'durable_refusals': []}))
    # ... and the same silence on the served tail alone.
    expect('served-silent', lambda record:
           record.update({'served_refusals': []}))
    # A refiring gate that journals once per scan floods the trail.
    expect('refusal-flooded', lambda record:
           record.update({'durable_refusals':
                          record['durable_refusals'] * 3}))
    # The row that names no cause names nothing an auditor can use.
    expect('refusal-unnamed', lambda record:
           record['durable_refusals'][0].update(
               {'error': {'field_claim_failed': {
                   'detail': 'ask failed'}}}))
    # A row fired below the declared budget mis-attributes the
    # boundary it names.
    expect('refusal-under-counted', lambda record:
           record['durable_refusals'][0].update({'misses': 80}))
    # The gate that promotes on a voided proof is the wrong answer —
    # worse than a silent one.
    expect('gate-promoted', lambda record:
           record['window'][2].update({'role': 'promoting'}))
    # A transition journaled beside the refusal is a move the
    # refused attempt never made.
    expect('role-walked', lambda record:
           record.update({'role_walks': [
               {'from': 'standby', 'to': 'promoting'}]}))
    # The armed-and-eligible fire that stays parked on a standing
    # proof is the availability miss the second half pins.
    expect('fire-parked', lambda record: (
        [row.update({'role': 'standby'})
         for row in record['fire_window']],
        record.update({'fire_promoted': None})))
    # A promotion the served role reports but the durable trail
    # never carries is the same silence the finding named, one
    # window later.
    expect('fire-unwalked', lambda record:
           record.update({'fire_walks': []}))
    # A second refusal row where the standing proof makes the fire
    # a promotion breaks the bounded record.
    expect('refusal-relapse', lambda record:
           record.update({'final_refusals':
                          record['final_refusals'] * 2}))
    # A gate that fires below the named boundary mis-attributes the
    # contract both halves share.
    expect('fire-early', lambda record:
           record['fire_window'][3].update({'misses': 40}))
    # The instability the contract does not answer for must report
    # nondeterministic, not failed: a starved watch, misses that
    # never reach the budget, a dropped served read, refused
    # staging calls.
    expect('watch-starved', lambda record:
           record.update({'window': []}), DIAG_NONDET)
    expect('boundary-unreached', lambda record: [
        row.update({'misses': 60})
        for row in record['window']], DIAG_NONDET)
    expect('served-dropped', lambda record:
           record.update({'served_refusals': None}), DIAG_NONDET)
    expect('demote-refused', lambda record:
           record.update({'demote': {'status': 409,
                                     'body': 'not_active'}}),
           DIAG_NONDET)
    expect('promote-refused', lambda record:
           record.update({'promote': {'status': 409,
                                      'body': 'not_converged'},
                          'owns': None}),
           DIAG_NONDET)
    # The second half's instability: the proof that never re-stands,
    # the starved eligible watch, the climb that stalls below the
    # boundary.
    expect('reconverge-lost', lambda record:
           record.update({'reconverged': None}), DIAG_NONDET)
    expect('fire-watch-starved', lambda record:
           record.update({'fire_window': []}), DIAG_NONDET)
    expect('fire-boundary-stalled', lambda record: [
        row.update({'role': 'standby', 'misses': 40})
        for row in record['fire_window']], DIAG_NONDET)
    return slipped


def _refusal_pass(ctx, number):
    """One voided-gate pass: promote the armed peer over the field,
    stop the launched owner, demote the armed peer onto its dead
    configured source, watch the produced-nothing misses climb to
    the declared budget and hold past the boundary, and audit the
    durable and served promotion_refused rows and the absent role
    walk. Then the eligible-fire half: restart the owner so the
    peer's pulls re-stand the convergence proof, stop it again so
    the budget-th miss arrives with the proof standing, and audit
    the promotion the armed gate owes — served role and journaled
    transition — beside the still-bounded refusal row, before the
    launch layout restores. Returns (record, evidence): the record
    is what the judge replays; an aborted stage simply leaves its
    later keys absent for the judge to name."""
    record = {}
    evidence = {'pass': number}

    row = _armed_row(ctx, 'standby')
    record['budget'] = (row or {}).get('budget')

    # The armed peer must own the field to be demoted onto the dead
    # source — the misordered-failover promote fences the incumbent
    # into an in-place demotion, the keyed-or-unkeyed switch.
    promoted, last = None, None
    deadline = time.monotonic() + REFUSAL_SETTLE
    while promoted is None and time.monotonic() < deadline:
        status, body = _settle_call(ctx['standby'] + '/promote')
        if status == 200:
            promoted = body
        else:
            last = (status, body)
            time.sleep(REFUSAL_POLL)
    record['promote'] = {'status': 200 if promoted is not None
                         else last[0] if last else None,
                         'body': promoted if promoted is not None
                         else last[1] if last else None}
    if promoted is not None:
        record['owns'] = wait_for(
            lambda: _pair_active(ctx) == 'standby' or None,
            time.monotonic() + REFUSAL_SETTLE, interval=REFUSAL_POLL)
    if promoted is None or not record.get('owns'):
        return record, evidence

    try:
        ctx['stop_controller']('active')

        # Void the proof under the armed gate: the demote lands the
        # peer back on its configured tracking source — the stopped
        # container — so every scan from here is a produced-nothing
        # miss counted against a proof that never re-stands.
        status, body = _settle_call(ctx['standby'] + '/demote')
        record['demote'] = {'status': status, 'body': body}
        if status != 200:
            return record, evidence
        record['settled'] = wait_for(
            lambda: (_try_role(ctx, ctx['standby']) or {})
                    .get('role') == 'standby' or None,
            time.monotonic() + REFUSAL_SETTLE, interval=REFUSAL_POLL)
        if not record['settled']:
            return record, evidence

        # The audit floor past the demote's own journaled settle:
        # everything the window produces lands after it.
        gate_floor = len(_journal_entries(
            ctx['journal_files']['standby']))
        gate_served = _journal_cursor(ctx, ctx['standby'])

        # The voided-gate climb: watch the armed peer while the
        # produced-nothing misses reach the declared budget — every
        # row must read standby — then hold past the boundary so a
        # refiring gate or a flooding trail has scans to land in.
        rows = []
        deadline = time.monotonic() + REFUSAL_WINDOW
        while time.monotonic() < deadline:
            row = _armed_row(ctx, 'standby')
            if row is not None:
                rows.append(row)
                if row.get('role') != 'standby' or (
                        isinstance(row.get('misses'), int)
                        and isinstance(row.get('budget'), int)
                        and row['misses'] >= row['budget']):
                    break
            time.sleep(REFUSAL_POLL)
        end = time.monotonic() + REFUSAL_HOLD
        while time.monotonic() < end:
            row = _armed_row(ctx, 'standby')
            if row is not None:
                rows.append(row)
            time.sleep(REFUSAL_POLL)
        record['window'] = rows
        record['durable_refusals'] = _refused_rows(
            ctx, 'standby', gate_floor)
        record['served_refusals'] = _served_refusals(
            ctx, 'standby', gate_served)
        record['role_walks'] = _role_walks(
            ctx, 'standby', gate_floor)

        # The armed-and-eligible fire the refused attempt must not
        # preclude: the owner's restart re-claims the released field
        # and the peer's pulls re-stand the convergence proof, so
        # the owner's second silence is a dead field owner with the
        # proof standing — the budget-th miss from here fires the
        # gate into the promotion the durable trail must carry
        # beside the one refusal row.
        ctx['start_controller']('active')
        record['reconverged'] = wait_for(
            lambda: (_pair_active(ctx) == 'active' or None)
            and _eligible_row(ctx),
            time.monotonic() + REFUSAL_SETTLE * 2,
            interval=REFUSAL_POLL)
        if not record['reconverged']:
            return record, evidence

        fire_floor = len(_journal_entries(
            ctx['journal_files']['standby']))
        ctx['stop_controller']('active')
        fire = []
        deadline = time.monotonic() + REFUSAL_WINDOW
        while time.monotonic() < deadline:
            row = _armed_row(ctx, 'standby')
            if row is not None:
                fire.append(row)
                if row.get('role') != 'standby':
                    break
            time.sleep(REFUSAL_POLL)
        record['fire_window'] = fire
        record['fire_promoted'] = wait_for(
            lambda: (_try_role(ctx, ctx['standby']) or {})
                    .get('role') == 'active' or None,
            time.monotonic() + REFUSAL_SETTLE,
            interval=REFUSAL_POLL)
        record['fire_walks'] = _role_walks(
            ctx, 'standby', fire_floor)
        record['final_refusals'] = _refused_rows(
            ctx, 'standby', gate_floor)
    finally:
        # The launched owner is stopped and the armed peer may own
        # the field — the documented demote-then-start order frees
        # the claim before the owner's container comes back; the
        # launch-layout verdict is audited wherever the pass ended.
        _restore_refusal_layout(ctx)
        record['restored'] = wait_for(
            lambda: (_pair_active(ctx) == 'active' or None)
            and _tracking_standby(ctx, 'standby'),
            time.monotonic() + REFUSAL_SETTLE * 2,
            interval=REFUSAL_POLL)
    return record, evidence


def _restore_refusal_layout(ctx):
    """Best-effort launch-layout restore: the launched owner back
    over the field with the armed peer tracking behind it — used by
    the layout gate and the cleanup path alike. The ordering the
    restart-safe switch owes the claim arbitration: a restarted
    owner only takes a *free* field, so an owning armed peer is
    demoted — its release frees the claim — before the stopped
    sibling's container is started again; and a mid-release demote
    is left to settle before the sibling is asked to claim.
    Everything retries inside the bound and swallows refusal."""
    try:
        deadline = time.monotonic() + REFUSAL_SETTLE * 2
        while time.monotonic() < deadline:
            owner = _try_role(ctx, ctx['active'])
            peer = _try_role(ctx, ctx['standby'])
            peer_role = (peer or {}).get('role')
            if peer_role == 'demoting':
                pass  # mid-release — let it settle
            elif peer_role in ('active', 'promoting'):
                # The armed peer still owns: its demote frees the
                # claim the restore hands back — safe once the
                # sibling is down (a restarted owner only takes a
                # free field) or promotable (a tracking standby
                # promotes the moment the claim frees).
                if owner is None or (
                        owner.get('role') == 'standby'
                        and 'tracking'
                        in (owner.get('sync') or {})):
                    _settle_call(ctx['standby'] + '/demote')
            elif owner is None:
                try:
                    ctx['start_controller']('active')
                except Exception:
                    pass
            elif owner.get('role') != 'active':
                _settle_call(ctx['active'] + '/promote')
            if (owner or {}).get('role') == 'active' \
                    and peer_role == 'standby' \
                    and 'tracking' in ((peer or {}).get('sync') or {}):
                return
            time.sleep(REFUSAL_POLL)
    except Exception:
        pass


def scenario_failover_refusal_journal(ctx):
    """Pin the fired-but-refused gate's durable trail on the run's
    launched pair: the armed standby promoted over the field, its
    configured tracking source stopped, and the peer demoted onto
    that dead source reproduces the finding's voided window — the
    evidence-free misses climb to the armed budget with the
    convergence proof already lapsed, the boundary fires the gate,
    and the refused attempt must journal exactly one
    promotion_refused naming the not_converged cause and the fired
    miss count on both the durable file and the served tail, beside
    no role transition; then the owner restarts to re-stand the
    proof and dies again, so a later armed-and-eligible fire must
    still promote — journaled, on the named boundary — the pair
    restored to its launch layout."""
    case = Case(
        'failover-refusal-journal',
        'A fired-but-refused failover journals its named refusal',
        'with the launched pair settled, each pass promotes the '
        'armed standby over the field — the incumbent\'s '
        'fencing-loss demotes it in place — stops the launched '
        'owner\'s container, and demotes the armed peer onto its '
        'configured dead source so produced-nothing misses reach '
        'the armed budget with the convergence proof already '
        'voided; the served role must stay standby through the '
        'window, the durable --journal-file and the served '
        '/journal tail must each hold exactly one '
        'promotion_refused entry naming the not_converged cause '
        'and the fired miss count, no ownership transition may '
        'journal beside it; the restarted owner then re-claims '
        'the field and re-stands the peer\'s proof, the owner '
        'dies again, and the armed-and-eligible fire at the '
        'budget must promote the peer — served and journaled — '
        'beside the still-single refusal row, the pair restored '
        'to its launch layout, and two passes produce identical '
        'digests')
    try:
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries only one endpoint — the '
                               'pair the refusal leg needs is '
                               'absent')
        for action in ('stop_controller', 'start_controller'):
            if ctx.get(action) is None:
                return case.finish('inconclusive', 'the run '
                                   'context carries no ' + action
                                   + ' action — the dead-source '
                                   'void cannot be driven')
        journals = ctx.get('journal_files') or {}
        if not (journals.get('standby')
                and Path(journals['standby']).is_file()):
            return case.finish('inconclusive', 'the run context '
                               'carries no journal file for the '
                               'armed peer — the durable '
                               'promotion_refused audit cannot run')
        for name in ('active', 'standby'):
            try:
                _role(ctx, ctx[name])
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s '
                                   'monitor is unreachable: '
                                   + str(exc)[:200])

        # The armed-gate subject: the launched standby is the
        # pair's only failover gate; a release serving no failover
        # evidence predates the contract this leg audits.
        armed = _armed_row(ctx, 'standby')
        if not isinstance((armed or {}).get('budget'), int):
            return case.finish('inconclusive', 'the launched '
                               'standby serves no armed failover '
                               'evidence — the staged release '
                               'predates the --auto-promote '
                               'contract this leg audits')

        # The launch layout the passes stage from: the unarmed
        # owner over the field, the armed peer tracking and
        # converged — a swapped or unsettled pair is restored
        # before the leg reports.
        deadline = time.monotonic() + REFUSAL_SETTLE
        if _pair_active(ctx) != 'active' \
                or _tracking_standby(ctx, 'standby') is None:
            _restore_refusal_layout(ctx)
        settled = wait_for(
            lambda: (_pair_active(ctx) == 'active' or None)
            and _tracking_standby(ctx, 'standby'),
            deadline, interval=REFUSAL_POLL)
        if settled is None:
            return case.finish('inconclusive', 'the pair never '
                               'settled on its launch layout — '
                               'the armed peer must own a '
                               'tracking, converged proof for '
                               'the voided window the leg '
                               'stages')
        case.observe('armed failover gate: standby (' + ctx['standby']
                     + '); dead-source window: active ('
                     + ctx['active'] + ')')

        digests = []
        try:
            for number in (1, 2):
                violations = {}

                def note(key, diagnostic, detail):
                    violations.setdefault(key, (diagnostic, detail))

                record, evidence = _refusal_pass(ctx, number)
                _judge_window(record, note)
                digest = _refusal_digest(violations)
                evidence['record'] = record
                evidence['digest'] = dict(digest)
                evidence['violations'] = {
                    key: diagnostic for key, (diagnostic, _)
                    in violations.items()}
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'failover-refusal-journal-pass-' + str(number)
                    + '.json', evidence)
                case.evidence('file', ref, 'failover-refusal '
                              'pass ' + str(number) + ' — the '
                              'arming switch, the dead-source '
                              'demote, the voided-window watch '
                              'rows, the durable and served '
                              'promotion_refused bodies, the '
                              'role-walk audit, the restore, and '
                              'the normalized digest')
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
            # clean pass restores it by construction; an aborted
            # pass gets the stopped peer restarted and the
            # documented role order run again, best-effort.
            _restore_refusal_layout(ctx)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', DIAG_NONDET + ': the two passes\' '
                'digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two voided-gate passes, identical digests: '
                     + json.dumps(digests[0], sort_keys=True))

        # The unchecked-diagnostic self-check: the refusal judge
        # replays each planted negative it must name; a silent
        # judge means the leg can no longer catch what it names.
        slipped = _refusal_self_check()
        if slipped:
            return case.finish('failed', DIAG_UNCHECKED
                               + ': planted negatives slipped the '
                               'leg\u2019s own audits: '
                               + ', '.join(slipped))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
