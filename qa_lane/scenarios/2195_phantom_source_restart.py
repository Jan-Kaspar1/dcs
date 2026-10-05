"""The phantom_source_restart acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the leg restores what it moves — the demote/promote cycle
# ends back on the launch roles with the field owner active and the
# launched standby tracking it — so it sits in the launch-layout
# window the announced-source cluster keeps, behind the realign leg
# whose own restore leaves the same layout standing and ahead of the
# failover case the layout is owed to.
RUNS_AFTER = frozenset({'scenario_tracker_realign_tick_order'})
RUNS_BEFORE = frozenset({'scenario_failover'})


# --------------------------------------------------------------------
# The same-generation source_restarted suppression contract — the
# per-revision lane evidence for the contract #694's consolidated fix
# establishes (WW-FND-004's named-evidence journal clause and
# WW-LCM-001's continuity): an uninterrupted same-generation
# checkpoint stream must never journal `source_restarted` — not on a
# demoted peer's first tracking pull where the source's checkpoint is
# simply one tick behind the run tick, and not on a mid-tracking
# backward realignment — while a genuine cold restart of the tracked
# source still journals the entry carrying its `resumed_at` evidence.
#
# The defect the run reproduces is the QA finding
# `demote-track-journals-phantom-source-restart`: the regression
# detector treated any checkpoint below the run's last alignment — or
# below the run tick itself where no alignment stood — as a source
# restart, so a demoted peer's first tracking pull and a
# same-generation peer merely lagging one scan both tripped it and
# filled the durable journal with restarts that never happened
# (seq 638 phantom with `was_aligned: null` on the demoted peer's
# own first pull; seq 639 the one-tick regression). The fix reads the
# checkpoint's `generation` stamp: the uninterrupted successor still
# stamps the generation the demoted run's own captures carried, so
# the reset was the peer's tracking state and nothing journals, while
# a source that genuinely cold-restarted mints a new generation and
# the boundary is preserved.
#
# Each pass stages the two halves on the deployed pair:
#
# - the demote half: with the pair settled and tracking, the field
#   owner demotes and the launched standby promotes; the demoted
#   owner then follows its successor and applies repeatedly, each
#   pull one scan behind the successor's stream. Through the demoted
#   peer's serving monitor and its durable `--journal-file` the pass
#   audits that no `source_restarted` entry appeared — neither on the
#   first apply, whose checkpoint trails the demoted run's own tick
#   because the demotion cleared the alignment, nor on the
#   subsequent one-tick-lag applies the repeated cycle opens.
# - the restart half: the tracked source's container is
#   cold-restarted (`ctx['cold_restart_controller']` — `docker stop`,
#   its host-side state.json dropped, `docker start`), so the resumed
#   process mints a fresh generation and serves a regressed stream.
#   The tracking peer must then journal exactly one
#   `source_restarted`, carrying its named evidence: the prior
#   alignment as `was_aligned` and the resumed stream tick below it
#   as `resumed_at`. Suppression scoped to one generation must not
#   silence the boundary it exists to preserve.
#
# The pass restores the pair's launch roles for the cases behind it:
# the restarted source's startup claim reclaims the field and the
# tracking standby reconverges onto it, so the documented
# demote/promote walk-back is only needed when the restart left a
# different owner standing.
#
# Named diagnostics: source-restart-evidence-failed tags the contract
# clauses — a same-generation pull that journals a phantom
# `source_restarted`, the genuine cold restart journaling none or
# more than one, an entry missing its `was_aligned`/`resumed_at`
# evidence or naming a resumed tick that never regressed, the
# tracking peer never reconverging on the restarted stream, the
# launch roles unrestored — and source-restart-evidence-nondeterministic
# tags the instability the contract does not answer for: a refused
# staging call, a starved watch, a demotion that never landed, a
# promotion that never converged, a restart that never produced a
# regressed stream, an armed failover firing inside the held window.
# A staged run that predates the contract — a served checkpoint
# carrying no `generation` stamp — reports inconclusive. The
# unchecked-diagnostic self-check replays the judge over planted
# negatives and reports phantom-source-restart-unchecked for any that
# slip through.

ORDER_SETTLE = 60   # bound on each demote, promote, demoted
                    # reconvergence, restart return, and restore wait
ORDER_POLL = 0.4    # cadence watching the pair mid-cycle
ORDER_HOLD = 30     # bound on the journaled restart landing
ORDER_ADOPT = 6     # repeated tracking applies after the demotion —
                    # the one-tick-lag window the mid-tracking clause
                    # audits
DIAG_FAILED = 'source-restart-evidence-failed'
DIAG_NONDET = 'source-restart-evidence-nondeterministic'
DIAG_UNCHECKED = 'phantom-source-restart-unchecked'


def _restart_rows(items):
    """The `source_restarted` events a journal carries, each as
    `{'seq', 'tick', 'was_aligned', 'resumed_at'}` — for a parsed
    `--journal-file`'s records and a served `GET /journal` tail
    alike."""
    found = []
    for item in items:
        body = item.get('entry') if isinstance(item.get('entry'), dict) \
            else item
        event = (body or {}).get('event') or {}
        restart = event.get('source_restarted')
        if not isinstance(restart, dict):
            continue
        found.append({'seq': body.get('seq'), 'tick': body.get('tick'),
                      'was_aligned': restart.get('was_aligned'),
                      'resumed_at': restart.get('resumed_at')})
    return found


def _served_restarts(ctx, base, floor=None):
    """The `source_restarted` entries a monitor's served journal
    carries — those past `floor` when one is named. None while the
    read drops: a lost observation, never the audit's verdict."""
    try:
        _, payload = http_json('GET', base + '/journal')
    except Exception:
        return None
    rows = _restart_rows(_journal_list(payload))
    if floor is not None:
        rows = [row for row in rows
                if isinstance(row.get('seq'), int) and row['seq'] > floor]
    return rows


def _served_mark(ctx, base):
    """The highest seq a monitor's served journal carries — the pass's
    own floor for the served census, so a restart an earlier pass
    journaled is never recounted."""
    try:
        _, payload = http_json('GET', base + '/journal')
    except Exception:
        return 0
    seqs = [row.get('seq') for row in _restart_rows(_journal_list(payload))]
    return max((seq for seq in seqs if isinstance(seq, int)), default=0)


def _stream_tick(ctx, base):
    """The served stream position a monitor's checkpoint declares —
    its `stream_tick` where the run carries a lead over the tracked
    line, its own run tick where it declares none. None while the
    read drops or the document carries no integer position."""
    try:
        _, checkpoint = http_json('GET', base + '/checkpoint')
    except Exception:
        return None
    tick = checkpoint.get('stream_tick')
    if not isinstance(tick, int) or isinstance(tick, bool):
        tick = checkpoint.get('tick')
    return tick if isinstance(tick, int) \
        and not isinstance(tick, bool) else None


def _stream_generation(ctx, base):
    """The `generation` a monitor's served checkpoint stamps — the
    stream's tick-domain identity the suppression reads. None while
    the read drops or the document predates the stamp."""
    try:
        _, checkpoint = http_json('GET', base + '/checkpoint')
    except Exception:
        return None
    generation = checkpoint.get('generation')
    return generation if isinstance(generation, int) \
        and not isinstance(generation, bool) else None


def _gained_restarts(path, floor):
    """The `source_restarted` entries the durable journal gained
    since `floor` records — file order is append order, the axis the
    audit walks."""
    return _restart_rows(_journal_entries(path)[floor:])


def _evidence_row(ctx, base):
    """One normalized watch row off a monitor's served `/role` — the
    observation the mid-cycle audit replays: the reported role, the
    sync verdict's kind, the aligned stream tick, and the run tick the
    attribution domain stands at. None when the peer does not
    answer."""
    report = _try_role(ctx, base)
    if report is None:
        return None
    sync = report.get('sync')
    kind = sync if isinstance(sync, str) else 'none'
    if isinstance(sync, dict) and sync:
        kind = next(iter(sync))
    return {'role': report.get('role'), 'sync': kind,
            'aligned': _tracking_aligned(report),
            'tick': report.get('tick')}


def _judge_restart_evidence(record, note):
    """Audit one pass's record — replayable, so the self-check can
    hand it planted negatives. `note(key, diagnostic, detail)`
    records each clause the record violates: DIAG_FAILED tags the
    contract clauses and DIAG_NONDET the instability the contract does
    not answer for. An aborted stage ends the audit where the pass
    ended — the later keys it never wrote are not clauses."""
    def failed(key, detail):
        note(key, DIAG_FAILED, detail)

    def nondet(key, detail):
        note(key, DIAG_NONDET, detail)

    if record.get('journal_error') is not None:
        nondet('journal-read', "the tracking peer's durable journal "
               'could not be read: ' + str(record['journal_error']))
        return
    # A stage that never landed is an instability, not a verdict: the
    # audit ends where the pass ended and the later keys it never
    # wrote are not clauses.
    if record.get('baseline') is None:
        nondet('watch', 'the pass never observed the tracking posture '
               'it stages from — its baseline read dropped')
        return
    rows = record.get('demoted') or []
    if not rows:
        nondet('watch', 'the demoted-tracking window collected no '
               'served rows — the starved monitor gave the audit '
               'nothing to read')
        return
    if record.get('demote_error') is not None:
        nondet('demote', 'the demotion never landed: '
               + str(record['demote_error']))
        return
    if record.get('promote_error') is not None:
        nondet('promote', 'the promotion never converged: '
               + str(record['promote_error']))
        return
    if record.get('restart_error') is not None:
        nondet('restart', 'the tracked source never cold-restarted: '
               + str(record['restart_error']))
        if not record.get('restored'):
            failed('roles', 'the pair never settled back to its launch '
                   'roles — ' + str(record.get('owner'))
                   + ' active with ' + str(record.get('peer'))
                   + ' tracking behind it')
        return
    stale = [row for row in rows
             if row.get('role') not in ('standby', 'demoting')]
    if stale:
        record['armed'] = True
        nondet('armed-window', 'the demoted peer left its standby role '
               'inside the held window — the armed failover boundary '
               'was reached where the leg stages a reset: '
               + json.dumps(stale[0])[:200])
    if not record.get('reconverged'):
        failed('reconverge', 'the demoted peer never reconverged '
               'tracking on its successor — the reset the clause '
               'distinguishes never happened: last row '
               + json.dumps(rows[-1] if rows else None)[:200])
    if record.get('served_phantom'):
        failed('phantom', 'the same-generation pull journaled '
               + str(len(record['served_phantom']))
               + ' source_restarted records on the serving monitor — '
               'a phantom restart the demote-to-track reset '
               'manufactures: '
               + json.dumps(record['served_phantom'][:3])[:300])
    if record.get('phantom'):
        failed('phantom-durable', 'the same-generation pull journaled '
               + str(len(record['phantom'])) + ' source_restarted '
               'records in the durable journal — the demoted peer '
               'attributed its own tracking reset to the source: '
               + json.dumps(record['phantom'][:3])[:300])
    if record.get('armed'):
        # The window the restart evidence is audited across never
        # held; the later keys are not clauses.
        if not record.get('restored'):
            failed('roles', 'the pair never settled back to its launch '
                   'roles — ' + str(record.get('owner')) + ' active '
                   'with ' + str(record.get('peer')) + ' tracking '
                   'behind it')
        return
    restarts = record.get('restarts')
    if not isinstance(restarts, list):
        nondet('restart-staged', 'the cold-restarted source served no '
               'regressed stream — the induction never crossed a run '
               'boundary, so the contract has no restart to judge')
    elif len(restarts) != 1:
        note('restart-entry',
             DIAG_FAILED if not restarts else DIAG_NONDET,
             'a genuinely cold-restarted source journals exactly one '
             'source_restarted entry, found ' + str(len(restarts))
             + ': ' + json.dumps(restarts[:3])[:300])
    else:
        entry = restarts[0]
        if not isinstance(entry.get('tick'), int):
            failed('restart-entry', 'the journaled restart carries no '
                   'integer tick — the attributed-record axis the '
                   'contract names evidence on is malformed: '
                   + json.dumps(entry)[:300])
        if not isinstance(entry.get('was_aligned'), int):
            failed('restart-evidence', 'the journaled restart names no '
                   'prior alignment (was_aligned '
                   + json.dumps(entry.get('was_aligned'))
                   + ') — the evidence a same-generation regression '
                   'would also carry is absent')
        if not isinstance(entry.get('resumed_at'), int):
            failed('restart-evidence', 'the journaled restart names no '
                   'resumed stream tick: ' + json.dumps(entry)[:300])
        elif isinstance(entry.get('was_aligned'), int) \
                and entry['resumed_at'] >= entry['was_aligned']:
            failed('restart-evidence', 'the journaled restart resumed '
                   'at ' + str(entry['resumed_at']) + ' where its prior '
                   'alignment stood at ' + str(entry['was_aligned'])
                   + ' — the stream never regressed, so the entry '
                   'claims a restart that did not happen')
    if not record.get('restored'):
        failed('roles', 'the pair never settled back to its launch '
               'roles — ' + str(record.get('owner')) + ' active with '
               + str(record.get('peer')) + ' tracking behind it')


def _restart_evidence_digest(violations):
    """The pass's normalized verdict record — identical across clean
    passes; each field is the clean value only while no violation —
    contract or instability — names its clause."""
    def clean(*keys):
        return not any(key in violations for key in keys)
    return {
        'demoted': 'tracking' if clean('reconverge')
            else 'stranded',
        'phantom': 'none' if clean('phantom', 'phantom-durable')
            else 'journaled',
        'restart': 'one' if clean('restart-entry',
                                  'restart-evidence',
                                  'restart-staged')
            else 'unproven',
        'roles': 'restored' if clean('roles')
            else 'unrestored'}


def _restart_evidence_self_check():
    """The leg's unchecked-diagnostic self-test: replay the evidence
    judge over each planted negative the issue names — the phantom
    entry asserted on a same-generation pull, the genuine restart
    asserted as unjournaled or duplicated, the entry stripped of its
    named evidence, the reconvergence that never came, the launch
    roles unrestored — and require the judge to note each; the
    instability shapes must report nondeterministic, not failed. A
    silent judge returns the negative names it let through."""
    slipped = []

    def clean_record():
        return {'owner': 'active', 'peer': 'standby',
                'baseline': {'role': 'standby', 'sync': 'tracking',
                             'aligned': 118, 'tick': 130},
                'demote_error': None, 'promote_error': None,
                'restart_error': None, 'journal_error': None,
                'armed': False,
                'demoted': [{'role': 'standby', 'sync': 'tracking',
                             'aligned': 117, 'tick': 131},
                            {'role': 'standby', 'sync': 'tracking',
                             'aligned': 119, 'tick': 133},
                            {'role': 'standby', 'sync': 'tracking',
                             'aligned': 121, 'tick': 135}],
                'reconverged': {'role': 'standby',
                                'sync': {'tracking': {'aligned': 122}}},
                'served_phantom': [], 'phantom': [],
                'restarts': [{'seq': 11, 'tick': 141,
                              'was_aligned': 122, 'resumed_at': 1}],
                'restored': {'role': 'standby',
                             'sync': {'tracking': {'aligned': 126}}}}

    def expect(name, mutate, diagnostic=DIAG_FAILED):
        record = clean_record()
        mutate(record)
        found = {}
        _judge_restart_evidence(
            record,
            lambda key, diag, detail: found.setdefault(key, diag))
        if diagnostic not in found.values():
            slipped.append(name)

    # The doctored negative the issue names first: a phantom entry
    # asserted on the same-generation pull — the served monitor's view.
    expect('phantom-served', lambda record: record.update(
        {'served_phantom': [{'seq': 9, 'tick': 131,
                             'was_aligned': None,
                             'resumed_at': 130}]}))
    # ... and its durable half.
    expect('phantom-durable', lambda record: record.update(
        {'phantom': [{'seq': 9, 'tick': 131, 'was_aligned': None,
                      'resumed_at': 130}]}))
    # The genuine restart asserted as unjournaled — the suppression
    # over-reaching — and duplicated.
    expect('restart-silent', lambda record: record.update(
        {'restarts': []}))
    expect('restart-twice', lambda record: record.update(
        {'restarts': [{'seq': 11, 'tick': 141, 'was_aligned': 122,
                       'resumed_at': 1},
                      {'seq': 12, 'tick': 142, 'was_aligned': 123,
                       'resumed_at': 2}]}), DIAG_NONDET)
    # The entry stripped of its named evidence, and one naming a
    # resumed tick that never regressed below its prior alignment.
    expect('evidence-absent', lambda record: record.update(
        {'restarts': [{'seq': 11, 'tick': 141, 'was_aligned': None,
                       'resumed_at': None}]}))
    expect('evidence-nonregressing', lambda record: record.update(
        {'restarts': [{'seq': 11, 'tick': 141, 'was_aligned': 122,
                       'resumed_at': 130}]}))
    # The axis malformed.
    expect('tick-malformed', lambda record: record.update(
        {'restarts': [{'seq': 11, 'tick': None, 'was_aligned': 122,
                       'resumed_at': 1}]}))
    # The reconvergence never came, and the launch roles unrestored.
    expect('never-reconverged', lambda record: record.update(
        {'reconverged': None}))
    expect('roles-unrestored', lambda record: record.update(
        {'restored': None}))
    # The demoted peer left standby inside the held window.
    expect('failover-fired', lambda record: record.update(
        {'armed': True, 'demoted': [
            {'role': 'promoting', 'sync': 'none',
             'aligned': None, 'tick': 132}]}), DIAG_NONDET)
    # The instability the contract does not answer for must report
    # nondeterministic: a refused staging call at each stage, a
    # dropped durable read, a starved watch, an unconverged baseline,
    # and an induction that produced no regressed stream.
    expect('demote-refused', lambda record: record.update(
        {'demote_error': 'POST /demote answered 409'}), DIAG_NONDET)
    expect('promote-refused', lambda record: record.update(
        {'promote_error': 'POST /promote answered 409'}), DIAG_NONDET)
    expect('restart-refused', lambda record: record.update(
        {'restart_error': 'docker stop failed'}), DIAG_NONDET)
    expect('journal-unreadable', lambda record: record.update(
        {'journal_error': 'journal file does not parse'}), DIAG_NONDET)
    expect('window-silent', lambda record: record.update(
        {'demoted': []}), DIAG_NONDET)
    expect('baseline-dropped', lambda record: record.update(
        {'baseline': None}), DIAG_NONDET)
    expect('restart-unproven', lambda record: record.update(
        {'restarts': None}), DIAG_NONDET)
    return slipped


def _restore_launch(ctx, owner, peer):
    """Best-effort launch-layout restore on the deployed pair: walk
    the pair back to the launch roles — the named owner holding the
    field, the sibling tracking behind it. Every step is retried
    inside the bound and swallowed on refusal."""
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


def _restart_evidence_pass(ctx, number, owner, peer):
    """One evidence pass: with the pair settled and tracking, demote
    the field owner and let the launched standby promote; drive the
    demoted peer through repeated tracking applies while polling its
    served role and durable journal for the phantom the reset
    manufactures; then cold-restart the tracked source's container so
    its stream regresses across a genuine run boundary, and audit the
    one `source_restarted` the tracking peer owes it. Returns
    (record, evidence): the record is what the judge replays; an
    aborted stage simply leaves its later keys absent for the judge
    to name."""
    record = {'owner': owner, 'peer': peer}
    evidence = {'pass': number, 'owner': owner, 'peer': peer}
    base, peer_base = ctx[owner], ctx[peer]
    # The audited journal is the demoted peer's own: the phantom the
    # demotion manufactures and the genuine restart the tracked
    # source's cold start owes it both journal on the tracking peer,
    # which after the cycle is the peer the leg demoted.
    journal_path = ctx['journal_files'][owner]

    try:
        floor = len(_journal_entries(journal_path))
    except Exception as exc:
        record['journal_error'] = str(exc)[:300]
        return record, evidence

    record['baseline'] = _evidence_row(ctx, peer_base)
    # The contract surface: the served checkpoint must carry the
    # `generation` stamp the suppression reads. A staged run predating
    # it cannot be judged — the judge names it.
    evidence['generation'] = _stream_generation(ctx, base)
    served_floor = _served_mark(ctx, base)

    # The demote half: the field owner releases the claim and its
    # alignment, the launched standby promotes, and the demoted peer
    # follows its successor. The extra owner scan before the demote
    # opens the one-tick lead the demoted run holds over the stream it
    # is about to pull.
    try:
        status, body = _settle_call(base + '/demote')
        record['demote_status'] = status
        if status != 200:
            record['demote_error'] = json.dumps(body)[:200]
            return record, evidence
    except Exception as exc:
        record['demote_error'] = str(exc)[:200]
        return record, evidence
    # The demoted run keeps scanning while the demotion settles, so it
    # stands a scan or two past what its successor will serve.
    for _ in range(2):
        _try_snapshot(ctx, base)
    deadline = time.monotonic() + ORDER_SETTLE
    while time.monotonic() < deadline:
        if (_try_role(ctx, peer_base) or {}).get('role') == 'active':
            break
        time.sleep(ORDER_POLL)
    else:
        record['promote_error'] = 'the launched standby never promoted'
        return record, evidence

    # The repeated applies: the demoted peer tracks its successor
    # across the whole window, each pull one scan behind the served
    # stream — the mid-tracking one-tick regression the second clause
    # names. The served monitor and the durable journal are read on
    # every poll so a phantom the moment it appears is the evidence.
    rows = []
    served_phantom = []
    for _ in range(ORDER_ADOPT):
        row = _evidence_row(ctx, base)
        if row is not None:
            rows.append(row)
            if row.get('role') not in ('standby', 'demoting'):
                record['armed'] = True
        served = _served_restarts(ctx, base, served_floor) or []
        served_phantom.extend(entry for entry in served
                              if entry not in served_phantom)
        _try_snapshot(ctx, peer_base)
        time.sleep(ORDER_POLL)
    record['demoted'] = rows
    record['reconverged'] = wait_for(
        lambda: _tracking_standby(ctx, owner),
        time.monotonic() + ORDER_SETTLE,
        interval=ORDER_POLL)
    record['served_phantom'] = served_phantom
    try:
        record['phantom'] = _gained_restarts(journal_path, floor)
    except Exception as exc:
        record['journal_error'] = str(exc)[:300]
        return record, evidence
    evidence['demoted'] = rows[:20]
    evidence['phantom'] = record['phantom'][:10]
    evidence['served_phantom'] = served_phantom[:10]

    # The restart half: the tracked source's container is
    # cold-restarted, so the resumed process mints a fresh generation
    # and serves a stream regressed below the tracking peer's
    # alignment — a genuine run boundary, the one the suppression
    # must not absorb.
    try:
        aligned = _tracking_aligned(_try_role(ctx, base) or {})
        if not isinstance(aligned, int):
            record['restart_error'] = (
                'the demoted peer reports no stream alignment before '
                'the restart — ' + json.dumps(_try_role(ctx, base)))
            return record, evidence
        record['aligned_before'] = aligned
        ctx['cold_restart_controller'](peer)
    except Exception as exc:
        # A restart that never landed is an instability the audit
        # names; the pass still owes the launch-role restore to the
        # cases behind it.
        record['restart_error'] = str(exc)[:300]
    # The resumed source serves from its own run boundary; its startup
    # claim reclaims the field and the tracking peer adopts the
    # regressed stream. The wait is for the regressed position itself:
    # a source that came back on a stream at or above its predecessor's
    # alignment never crossed a boundary, and the audit has none to
    # judge.
    stream_position = wait_for(
        lambda: (position if (position := _stream_tick(
            ctx, ctx[peer])) is not None
            and position < aligned else None),
        time.monotonic() + ORDER_SETTLE, interval=ORDER_POLL)
    record['resumed_at'] = stream_position

    def journaled():
        try:
            gained = _gained_restarts(journal_path, floor)
        except Exception as exc:
            record['journal_error'] = str(exc)[:300]
            return None
        if gained:
            record['restarts'] = gained
            return gained
        return None

    wait_for(journaled, time.monotonic() + ORDER_HOLD,
             interval=ORDER_POLL)
    if 'restarts' not in record:
        # The induction's own gate: the resumed source must serve a
        # stream below the alignment its predecessor published, or the
        # restart never regressed anything and the contract has no
        # boundary to judge.
        record['restarts'] = None if stream_position is None \
            or stream_position >= aligned else []
    evidence['restarts'] = record['restarts'][:5] \
        if isinstance(record['restarts'], list) else None
    evidence['aligned_before'] = record.get('aligned_before')

    # The launch roles: the restart left the respawned source holding
    # the field, so the documented order walks the pair back — the
    # successor's demote, the original owner's promote — and the next
    # pass (and the cases behind this one) meet the launch layout.
    _restore_launch(ctx, owner, peer)
    record['restored'] = wait_for(
        lambda: (_pair_active(ctx) == owner and 'active' or None)
        and _tracking_standby(ctx, peer),
        time.monotonic() + ORDER_SETTLE, interval=ORDER_POLL)
    return record, evidence


def scenario_phantom_source_restart(ctx):
    """Exercise the same-generation source_restarted suppression
    contract on the deployed pair: with the pair settled and tracking,
    demote the field owner and let the demoted peer track it across
    repeated applies, where each pull trails the served stream by a
    scan; through its serving monitor and durable journal the demoted
    peer must journal no source_restarted — the demotion's own
    tracking reset, not the source's restart. The tracked source's
    container is then cold-restarted so its stream regresses across a
    genuine run boundary, and the tracking peer must journal exactly
    one source_restarted carrying its named evidence. The pair's
    launch roles restore; two passes produce identical digests."""
    case = Case(
        'phantom-source-restart',
        'Same-generation checkpoint pulls journal no source restart',
        'with the deployed pair settled and tracking, the field owner '
        'is demoted and the launched standby promoted, so the demoted '
        'peer tracks its uninterrupted successor across repeated '
        'applies — each pull trailing the served stream by a scan — '
        'and through its serving monitor and durable journal no '
        'source_restarted may appear: the demotion cleared the peer\'s '
        'own alignment, not the source\'s identity. The tracked '
        'source\'s container is then cold-restarted so its checkpoint '
        'stream regresses across a genuine run boundary, and the '
        'tracking peer must journal exactly one source_restarted '
        'carrying its prior alignment and the resumed stream tick; the '
        'pair\'s launch roles restore and two passes produce '
        'identical digests')
    try:
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries only one endpoint — the pair '
                               'the suppression leg needs is absent')
        if ctx.get('cold_restart_controller') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no cold-restart action — the '
                               'genuine source restart cannot be '
                               'induced')
        journals = ctx.get('journal_files') or {}
        if not journals.get('active') or not journals.get('standby') \
                or not Path(journals['active']).is_file():
            return case.finish('inconclusive', 'the run context '
                               'carries no per-controller journal '
                               'files — the durable evidence cannot be '
                               'audited')
        for name in ('active', 'standby'):
            try:
                _role(ctx, ctx[name])
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s '
                                   'monitor is unreachable: '
                                   + str(exc)[:200])

        # The launch layout the passes stage from: the unconfigured
        # peer holds the field and the launched standby tracks it. A
        # swapped layout is walked back before the leg reports.
        owner, peer = 'active', 'standby'
        if _pair_active(ctx) != owner:
            _restore_launch(ctx, owner, peer)
        deadline = time.monotonic() + ORDER_SETTLE
        if wait_for(lambda: _pair_active(ctx) == owner
                    and 'active' or None, deadline,
                    interval=ORDER_POLL) != owner:
            reports = {name: _try_role(ctx, ctx[name])
                       for name in ('active', 'standby')}
            if all(report is None for report in reports.values()):
                return case.finish('inconclusive', 'the pair is '
                                   'unreachable — monitor endpoints '
                                   + ctx['active'] + ' and '
                                   + ctx['standby'])
            return case.finish('inconclusive', 'the pair never '
                               'settled on its launch layout — the '
                               'unconfigured peer must hold the field '
                               'for the demote the leg stages')
        if wait_for(lambda: _tracking_standby(ctx, peer),
                    deadline, interval=ORDER_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the demoted '
                               'peer\'s reconvergence the leg stages '
                               'has no tracking peer')

        # The contract surface: the field owner's served checkpoint
        # must carry the `generation` stamp the suppression reads. A
        # staged run whose documents predate it cannot be judged.
        generation = _stream_generation(ctx, ctx[owner])
        if generation is None:
            return case.finish('inconclusive', 'the field owner\'s '
                               'served checkpoint carries no generation '
                               'stamp — the staged run predates the '
                               'suppression contract the leg audits')
        case.observe('tracked line generation: ' + hex(generation))
        case.observe('field owner: ' + owner + ' (' + ctx[owner]
                     + '); demoted peer under audit: ' + peer)

        digests = []
        try:
            for number in (1, 2):
                violations = {}

                def note(key, diagnostic, detail):
                    violations.setdefault(key, (diagnostic, detail))

                record, ev = _restart_evidence_pass(
                    ctx, number, owner, peer)
                _judge_restart_evidence(record, note)
                digest = _restart_evidence_digest(violations)
                ev['record'] = record
                ev['digest'] = dict(digest)
                ev['violations'] = {
                    key: diagnostic for key, (diagnostic, _)
                    in violations.items()}
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'phantom-source-restart-pass-' + str(number)
                    + '.json', ev)
                case.evidence('file', ref, 'evidence pass '
                              + str(number) + ' — the demote/promote '
                              'cycle, the demoted peer\'s repeated '
                              'applies and phantom census, the '
                              'cold-restarted source\'s journaled '
                              'restart, the restore, and the '
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
            # gets the documented role order run again, best-effort.
            _restore_launch(ctx, owner, peer)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', DIAG_NONDET + ': the two passes\' digests '
                'diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two demote/restart passes, identical digests: '
                     + json.dumps(digests[0], sort_keys=True))

        # The unchecked-diagnostic self-check: the evidence judge
        # replays each planted negative it must name; a silent judge
        # means the leg can no longer catch what it names.
        slipped = _restart_evidence_self_check()
        if slipped:
            return case.finish('failed', DIAG_UNCHECKED
                               + ': planted negatives slipped the '
                               'leg’s own audits: '
                               + ', '.join(slipped))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))


