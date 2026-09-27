"""The failover_proof_report acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: The failover-proof-report leg runs behind the
# attributed-switch case's restore — the armed peer it partitions and
# promotes is the launched standby, and the entry expects the launch
# layout that restore leaves standing.
RUNS_AFTER = frozenset({'scenario_attributed_switch'})


# --------------------------------------------------------------------
# The served failover-proof exposure (WW-LCM-001's observability
# clause — the per-revision lane evidence for the two-promotion-gates
# decision's recorded contract addition, #1029): the two promotion
# gates read different evidence by design — a requested POST /promote
# gates on the fresh `sync` verdict while the armed self-promotion
# gate reads the standing convergence proof bounded by the armed
# heartbeat budget — and the role report must make that divergence
# legible. An armed peer's GET /role carries `failover`, the standing
# proof (`converged`) bundled with the miss accounting it is read
# against (`misses` of the armed `budget`): inside a miss window the
# report reads "verdict degraded, proof stands, misses k of N", and
# at the N-th miss the armed gate fires and the peer self-promotes,
# journaled origin=failover with no actor. An unarmed peer serves no
# accounting at all — the record's "no bare flag without its bound":
# the flag alone survives unbounded misses, so the proof is served
# only with the armed bound — and a payload shaped before the field
# existed loads and re-serves unchanged under the served contract's
# optional-field convention.
#
# The leg partitions the armed standby's checkpoint source — the
# controller stop/start lever, the lane's partition convention: the
# fetch path goes dead while every other endpoint keeps serving — and
# samples the standby's served /role through the degraded window,
# asserting the miss accounting climbs k of N beside the standing
# proof until the armed boundary fires and the peer self-promotes,
# the durable journal recording the failover-origin walk from
# scratch. The unarmed duty peer's report carries no accounting
# field, and a pre-field-shaped payload the leg's consumer
# reconstruction loads re-serves unchanged. Restore puts the launch
# layout back so the cases behind this one hold. The named
# diagnostics are failover-proof-report-failed and
# failover-proof-report-nondeterministic; two consecutive passes must
# produce identical digests. The leg reports inconclusive — never a
# false red — when the staged release predates the contract (the
# armed peer serves no `failover` at baseline) or the partition
# lever is unavailable.

PROOF_POLL = 0.3            # cadence sampling the armed standby's /role
PROOF_SETTLE = 45           # bound on a settle or reconvergence wait
PROOF_FAILOVER = 90         # bound on the armed gate's self-promotion
PROOF_JOURNAL = 30          # bound on the durable record landing
PROOF_RESTORE = 15          # grace the finally gives a pending restore


def _sync_kind(report):
    """The named convergence verdict a /role report carries —
    'tracking', 'degraded', 'unsynchronized', ... — or None where the
    report is field-owning or carries no sync at all."""
    sync = (report or {}).get('sync')
    if isinstance(sync, dict):
        return next(iter(sync), None)
    return sync


def _failover_gate(report):
    """The served failover accounting a /role report carries — the
    armed peer's {converged, misses, budget} bundle — or None on an
    unarmed peer and on a payload shaped before the field existed:
    the consumer's decode under the contract's optional-field
    convention."""
    return (report or {}).get('failover')


def _gate_shape(gate):
    """The served accounting's shape check: {converged: bool,
    misses: int, budget: int}. A present but wrong-shaped field is a
    contract violation, never a predating release."""
    return isinstance(gate, dict) \
        and isinstance(gate.get('converged'), bool) \
        and isinstance(gate.get('misses'), int) \
        and not isinstance(gate.get('misses'), bool) \
        and isinstance(gate.get('budget'), int) \
        and not isinstance(gate.get('budget'), bool)


def _decode_role_report(payload):
    """The consumer's reconstruction of a served /role payload under
    the optional-field convention the served contract declares: every
    optional field decodes to None where absent, and re-encoding
    drops the Nones — so a payload shaped before the failover field
    existed loads and re-serves unchanged."""
    if not isinstance(payload, dict):
        raise ValueError('not a role report: '
                         + json.dumps(payload)[:200])
    return {name: payload.get(name)
            for name in ('role', 'tick', 'sync',
                         'field_claim', 'failover')}


def _reserialize_role_report(decoded):
    """The re-served wire shape of a decoded report: optional fields
    absent where they decoded None — the skip-when-absent rule the
    pre-field payload's unchanged load relies on."""
    return {name: value for name, value in decoded.items()
            if value is not None}


def _proof_role_stream(journal_path, floor=0):
    """The durable `role_changed` entries the armed peer's journal
    file gained since `floor` — (seq, tick, from, to, origin, keys)
    in seq order."""
    stream = []
    for item in _journal_entries(journal_path)[floor:]:
        entry = item.get('entry') or {}
        change = (entry.get('event') or {}).get('role_changed')
        if change is not None:
            stream.append((entry.get('seq'), entry.get('tick'),
                           change.get('from'), change.get('to'),
                           change.get('origin'),
                           sorted(change.keys())))
    return stream


def _failover_proof_pass(ctx, base_a, base_b, journal_b, budget):
    """One partition -> window -> boundary -> record -> restore pass
    over the launch-layout pair.

    Stops the duty peer — the lane's partition convention, the
    checkpoint path the armed standby's heartbeat pulls goes dead —
    then samples the armed standby's served /role through the
    degraded window asserting the standing proof beside the miss
    accounting k of N, observes the armed boundary's self-promotion
    and audits the durable journal for the failover-origin walk, and
    restores the launch layout. Returns (digest, violations,
    evidence): digest is the pass's normalized verdict record,
    identical across clean passes; violations is {key: (diagnostic,
    detail)} in first-seen order."""
    violations = {}
    evidence = {'armed': base_b, 'duty': base_a,
                'declared_budget': budget}

    def note(key, diagnostic, detail):
        violations.setdefault(key, (diagnostic, detail))

    def failed(key, detail):
        note(key, 'failover-proof-report-failed', detail)

    def nondeterministic(key, detail):
        note(key, 'failover-proof-report-nondeterministic', detail)

    baseline = unarmed = decoded = reserialized = pre_field = None
    window = []
    misses_seen = []
    boundary_report = settled = restored = None
    stream = []
    try:
        # ---- baseline: the armed tracking report's accounting ----
        baseline = wait_for(
            lambda: _tracking_standby(ctx, 'standby'),
            time.monotonic() + PROOF_SETTLE,
            interval=PROOF_POLL)
        if baseline is None:
            raise ConnectionError(
                'the armed peer is not a tracking standby at pass '
                'start — the induction has no observation point: '
                + json.dumps(_try_role(ctx, base_b))[:200])
        evidence['baseline'] = baseline
        gate = _failover_gate(baseline)
        if gate is None:
            raise RuntimeError(
                'the armed peer serves no failover accounting on '
                'its tracking report — the staged release predates '
                'the role-report-failover-proof contract: '
                + json.dumps(baseline)[:300])
        if not _gate_shape(gate):
            failed('baseline-shape', 'the served failover '
                   'accounting is malformed — present but not the '
                   '{converged, misses, budget} bundle: '
                   + json.dumps(gate)[:200])
        else:
            if gate['budget'] != budget:
                failed('baseline-bound', 'the served budget '
                       + str(gate['budget']) + ' is not the armed '
                       + str(budget) + ' — the bound the proof is '
                       'read against is not the declared one')
            if gate['converged'] is not True:
                nondeterministic('baseline-proof', 'a tracking peer '
                                 'serves the proof flag lowered: '
                                 + json.dumps(gate)[:200])
            if gate['misses'] != 0:
                nondeterministic('baseline-misses', 'a tracking '
                                 'peer serves misses '
                                 + str(gate['misses']) + ' — the '
                                 'accounting is not from scratch '
                                 'ahead of the induction')

        # ---- the unarmed peer: no accounting field at all ----
        unarmed = _try_role(ctx, base_a)
        evidence['unarmed'] = unarmed
        if unarmed is None:
            raise ConnectionError('the duty peer never answered '
                                  '/role — the unarmed half has no '
                                  'observation point')
        if 'failover' in unarmed:
            failed('unarmed-accounting', 'the unarmed peer serves '
                   'failover accounting — the bare flag without '
                   'its bound the record forbids: '
                   + json.dumps(unarmed.get('failover'))[:200])

        # ---- the pre-field payload: loads unchanged ----
        pre_field = {name: value for name, value in baseline.items()
                     if name != 'failover'}
        try:
            decoded = _decode_role_report(pre_field)
            reserialized = _reserialize_role_report(decoded)
        except Exception as exc:
            failed('pre-field-load', 'the consumer reconstruction '
                   'refused a pre-field-shaped payload: '
                   + str(exc)[:200])
        evidence['pre_field'] = {'payload': pre_field,
                                 'decoded': decoded,
                                 'reserialized': reserialized}
        if decoded is not None:
            if decoded.get('failover') is not None:
                failed('pre-field-gate', 'a pre-field-shaped '
                       'payload decoded failover evidence — the '
                       'absent field must read as no accounting, '
                       'not a stood proof')
            if reserialized != pre_field:
                failed('pre-field-drift', 'the reconstructed '
                       'report re-serves changed — a pre-field '
                       'payload must load and re-serve '
                       'unchanged: ' + json.dumps(pre_field)[:200]
                       + ' -> ' + json.dumps(reserialized)[:200])

        floor = len(_journal_entries(journal_b))
        evidence['journal_floor'] = floor

        # ---- the induction: the lane's partition convention ----
        try:
            ctx['stop_controller']('active')
        except Exception as exc:
            raise ConnectionError('the checkpoint-source stop '
                                  'induction never completed — the '
                                  'partition lever is unavailable: '
                                  + str(exc)[:300])

        # ---- the degraded window: k of N beside the proof ----
        boundary_report = None
        deadline = time.monotonic() + PROOF_FAILOVER
        while time.monotonic() < deadline and boundary_report is None:
            report = _try_role(ctx, base_b)
            if report is None:
                time.sleep(PROOF_POLL)
                continue
            if not isinstance(report, dict):
                failed('report-shape', 'the armed peer\'s /role '
                       'answered a non-report payload: '
                       + json.dumps(report)[:200])
                break
            gate = _failover_gate(report)
            row = {'role': report.get('role'),
                   'sync': _sync_kind(report),
                   'converged': gate.get('converged')
                   if isinstance(gate, dict) else None,
                   'misses': gate.get('misses')
                   if isinstance(gate, dict) else None,
                   'budget': gate.get('budget')
                   if isinstance(gate, dict) else None}
            window.append(row)
            if report.get('role') == 'standby':
                if gate is None:
                    failed('accounting-suppressed', 'the armed '
                           'peer\'s report dropped the failover '
                           'accounting inside the miss window — '
                           'the standing proof is served through '
                           'the window, not only ahead of it: '
                           + json.dumps(report)[:200])
                    break
                if not _gate_shape(gate):
                    failed('window-shape', 'the mid-window '
                           'accounting is malformed: '
                           + json.dumps(gate)[:200])
                    break
                if gate['budget'] != budget:
                    failed('window-bound', 'the mid-window bound '
                           + str(gate['budget']) + ' is not the '
                           'armed ' + str(budget))
                    break
                if row['sync'] == 'tracking' \
                        and gate['misses'] == 0:
                    # The induction's first uncounted scans — the
                    # pull cycle has not run a miss yet.
                    pass
                elif row['sync'] == 'tracking':
                    nondeterministic(
                        'partition-leaky', 'the standby reports '
                        'tracking through the stopped source — a '
                        'checkpoint landed inside the partition')
                elif row['sync'] != 'degraded':
                    failed('window-verdict', 'the in-window '
                           'verdict is ' + str(row['sync'])
                           + ', not the produced-nothing pull\'s '
                           'degraded: ' + json.dumps(report)[:200])
                if misses_seen and gate['misses'] < misses_seen[-1]:
                    nondeterministic(
                        'miss-run-reset', 'the miss accounting '
                        'rewound mid-window '
                        + str(misses_seen[-1]) + ' -> '
                        + str(gate['misses']) + ' — a pull landed '
                        'through the partition')
                elif gate['misses'] >= 1 \
                        and (not misses_seen
                             or gate['misses'] > misses_seen[-1]):
                    misses_seen.append(gate['misses'])
                if gate['misses'] > budget:
                    failed('never-fired', 'the armed gate let the '
                           'miss run pass the declared budget — '
                           + str(gate['misses']) + ' of '
                           + str(budget) + ' with no promotion')
                    break
                if row['sync'] == 'degraded' \
                        and gate['converged'] is not True:
                    failed('proof-voided-early', 'the served '
                           'proof read voided at misses '
                           + str(gate['misses']) + ' of '
                           + str(budget) + ' — inside the armed '
                           'bound the proof must stand')
            else:
                boundary_report = report
            time.sleep(PROOF_POLL)
        evidence['window'] = window
        evidence['misses'] = misses_seen
        if misses_seen and len(misses_seen) < 2:
            nondeterministic('thin-window', 'the in-window '
                             'accounting served only '
                             + str(misses_seen) + ' — the k-of-N '
                             'legibility went unobserved')

        # ---- the armed boundary: the gate fires at the N-th miss
        evidence['boundary'] = boundary_report
        if boundary_report is None and not violations:
            failed('never-fired', 'the armed standby never left '
                   'standby inside the failover bound — the miss '
                   'run reached '
                   + str(misses_seen[-1] if misses_seen else 0)
                   + ' of ' + str(budget))
        elif boundary_report is not None:
            gate = _failover_gate(boundary_report)
            if boundary_report.get('role') \
                    not in ('promoting', 'active'):
                failed('off-contract-role', 'the peer left '
                       'standby as '
                       + str(boundary_report.get('role')))
            if not _gate_shape(gate):
                failed('boundary-accounting', 'the fired gate\'s '
                       'report carries no well-formed '
                       'accounting: '
                       + json.dumps(boundary_report)[:200])
            else:
                if gate['misses'] != budget:
                    failed('off-budget-fire', 'the gate fired at '
                           'misses ' + str(gate['misses'])
                           + ' — the armed boundary is the '
                           + str(budget) + '-th miss exactly')
                if gate['converged'] is not True:
                    failed('voided-fire', 'the gate fired with '
                           'the proof voided — the boundary '
                           'reads the standing proof, not the '
                           'verdict')
            if not misses_seen:
                nondeterministic('no-window-read', 'the '
                                 'promotion arrived ahead of '
                                 'the leg\'s first in-window '
                                 'read — the accounting went '
                                 'unobserved')
            deadline = time.monotonic() + PROOF_SETTLE
            while time.monotonic() < deadline and settled is None:
                report = _try_role(ctx, base_b)
                if (report or {}).get('role') == 'active':
                    settled = report
                else:
                    time.sleep(PROOF_POLL)
            evidence['settled'] = settled
            if settled is None:
                failed('never-settled', 'the self-promoted peer '
                       'never settled to active')

        # ---- the record: the failover-origin walk, from scratch --
        if settled is not None or boundary_report is not None:
            deadline = time.monotonic() + PROOF_JOURNAL
            while time.monotonic() < deadline and len(stream) < 2:
                stream = _proof_role_stream(journal_b, floor)
                if len(stream) < 2:
                    time.sleep(PROOF_POLL)
        evidence['journal'] = stream
        pairs = [(frm, to) for _s, _t, frm, to, _o, _k in stream]
        if stream or boundary_report is not None:
            if pairs != [('standby', 'promoting'),
                         ('promoting', 'active')]:
                failed('record-walk', 'the durable journal '
                       'gained the role walk '
                       + json.dumps(pairs) + ' — expected '
                       'standby->promoting->active recorded '
                       'from scratch')
            else:
                for seq, tick, frm, to, origin, keys in stream:
                    if origin != 'failover':
                        failed('record-origin', 'the ' + frm
                               + '->' + to + ' transition '
                               'journaled origin '
                               + json.dumps(origin)
                               + ' — the armed gate\'s switch '
                               'must record failover, not a '
                               'bare-asserted request')
                    if 'actor' in keys:
                        failed('record-actor', 'the ' + frm
                               + '->' + to + ' transition '
                               'carries an operator actor — '
                               'the automatic path records '
                               'no requester')

        # ---- restore: the launch layout ----
        # The promoted peer's demotion yields the field's claim
        # ahead of the duty peer's warm resume — a resume into a
        # live claim exits FieldClaimFailed — then the resumed
        # active's checkpoint stream reconverges the demoted
        # standby.
        restore_detail = None
        if settled is not None:
            status, body = _settle_call(base_b + '/demote')
            if status != 200:
                restore_detail = 'the restore demote answered ' \
                    + str(status) + ': ' + json.dumps(body)[:200]
            else:
                try:
                    ctx['start_controller']('active')
                except Exception as exc:
                    restore_detail = 'the duty peer\'s restart ' \
                        'never completed: ' + str(exc)[:200]
                else:
                    deadline = time.monotonic() + PROOF_SETTLE
                    while time.monotonic() < deadline \
                            and restored is None:
                        owner = _try_role(ctx, base_a)
                        peer = _try_role(ctx, base_b)
                        if (owner or {}).get('role') == 'active' \
                                and (peer or {}).get('role') \
                                == 'standby' \
                                and _sync_kind(peer) == 'tracking':
                            restored = {'owner': owner,
                                        'standby': peer}
                        else:
                            time.sleep(PROOF_POLL)
                    if restored is None:
                        restore_detail = 'the pair never settled ' \
                            'back to its launch layout'
        if restore_detail is not None:
            failed('restore', restore_detail)
    finally:
        # Best effort: put the launch layout back for the legs
        # behind this one — a pass that ended anywhere mid-episode
        # still leaves the rig it found. The demote yields any
        # promoted claim ahead of the duty peer's resume; a stop
        # that never fired needs only the start.
        owner = _try_role(ctx, base_a)
        peer = _try_role(ctx, base_b)
        launch_layout = (owner or {}).get('role') == 'active' \
            and (peer or {}).get('role') == 'standby' \
            and _sync_kind(peer) == 'tracking'
        if not launch_layout:
            try:
                if (peer or {}).get('role') \
                        in ('promoting', 'active'):
                    _settle_call(base_b + '/demote')
                ctx['start_controller']('active')
                deadline = time.monotonic() + PROOF_RESTORE
                while time.monotonic() < deadline:
                    owner = _try_role(ctx, base_a)
                    peer = _try_role(ctx, base_b)
                    if (owner or {}).get('role') == 'active' \
                            and (peer or {}).get('role') \
                            == 'standby' \
                            and _sync_kind(peer) == 'tracking':
                        if restored is None:
                            restored = {'owner': owner,
                                        'standby': peer}
                        break
                    time.sleep(PROOF_POLL)
            except Exception:
                pass  # a stranded rig is the pass's own verdict
    evidence['restored'] = restored

    def _window_row_ok(row, leading):
        """Whether one standby-phase observation carries the
        contract's window shape: the degraded verdict beside the
        standing proof and the bounded miss count — or, while
        `leading`, the induction's uncounted prefix (tracking,
        misses 0) ahead of the first counted miss."""
        if row['role'] != 'standby':
            return True
        if row['sync'] == 'tracking' and row['misses'] == 0:
            return leading
        return row['sync'] == 'degraded' \
            and row['converged'] is True \
            and isinstance(row['misses'], int) \
            and 1 <= row['misses'] <= budget

    clean_window = bool(misses_seen)
    leading = True
    for row in window:
        if row['role'] == 'standby' and row['sync'] == 'degraded':
            leading = False
        if not _window_row_ok(row, leading):
            clean_window = False
            break
    fired_at_budget = isinstance(
        _failover_gate(boundary_report or {}), dict) \
        and _failover_gate(boundary_report)['misses'] == budget \
        and _failover_gate(boundary_report)['converged'] is True \
        and (boundary_report or {}).get('role') \
        in ('promoting', 'active')
    record_ok = [(frm, to)
                 for _s, _t, frm, to, _o, _k in stream] \
        == [('standby', 'promoting'), ('promoting', 'active')] \
        and all(origin == 'failover' and 'actor' not in keys
                for _s, _t, _f, _t2, origin, keys in stream)
    evidence['digest'] = {
        'baseline': 'served' if _gate_shape(
            _failover_gate(baseline)) else 'malformed',
        'unarmed': 'absent' if 'failover' not in (unarmed or {})
                   else 'served',
        'pre_field': 'unchanged'
                     if reserialized is not None
                     and reserialized == pre_field else 'drifted',
        'window': 'k-of-n-proof-standing' if clean_window
                  else 'breached',
        'boundary': 'fired-at-budget' if fired_at_budget
                    else ('never-fired' if boundary_report is None
                          else 'off-budget'),
        'record': 'failover-origin' if record_ok else 'unrecorded',
        'restore': 'launch-layout' if restored else 'unsettled'}
    return evidence['digest'], violations, evidence


def scenario_failover_proof_report(ctx):
    """The armed standby's served failover accounting: the /role
    report must carry the standing proof beside the miss count k of
    the armed budget N through the degraded window, fire the armed
    gate at the N-th miss into a journaled failover-origin
    self-promotion, while the unarmed peer serves no accounting
    field and a pre-field-shaped payload loads unchanged."""
    case = Case('failover-proof-report',
                'The served /role carries the standing proof and '
                'the miss accounting through the failover window',
                'with the pair settled and tracking under the '
                'armed failover budget, stopping the duty peer '
                'leaves the armed standby\'s /role reporting sync '
                'degraded beside failover {converged, misses k, '
                'budget N} — the standing proof with its bound — '
                'while misses climb to the armed boundary, where '
                'the gate fires and the peer self-promotes with '
                'the durable journal recording '
                'standby->promoting->active origin failover and '
                'no actor; the unarmed peer\'s report carries no '
                'accounting field, a pre-field-shaped payload the '
                'consumer reconstructs loads unchanged, the '
                'launch layout returns, and two consecutive '
                'passes produce identical digests')
    try:
        stop = ctx.get('stop_controller')
        start = ctx.get('start_controller')
        if stop is None or start is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no controller stop/start '
                               'action — the partition lever is '
                               'unavailable')
        journals = ctx.get('journal_files') or {}
        journal_b = journals.get('standby')
        if journal_b is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no durable journal file '
                               'for the armed peer — the failover '
                               'record the leg audits is '
                               'unavailable')
        budget = ctx.get('failover_misses')
        if not isinstance(budget, int) or isinstance(budget, bool) \
                or budget < 1:
            return case.finish('inconclusive', 'the run declares '
                               'no armed miss budget — the armed '
                               'gate has nothing to exercise')
        base_a, base_b = ctx.get('active'), ctx.get('standby')
        if base_a is None or base_b is None:
            return case.finish('inconclusive', 'the pair ctx '
                               'names no endpoints to exercise')
        if ctx.get('evidence_dir') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no evidence directory')

        # The entry layout: the armed peer is the launched standby,
        # the unarmed peer the launched active. Whichever role
        # layout the suite hands this leg, the documented
        # demote/promote order puts the launch arrangement back —
        # the armed peer tracking its configured source, the duty
        # peer owning the field.
        owner = wait_for(lambda: _pair_active(ctx),
                         time.monotonic() + 30)
        if owner is None:
            reachable = any(
                _try_role(ctx, ctx[name]) is not None
                for name in ('active', 'standby') if ctx.get(name))
            return case.finish(
                'failed' if reachable else 'inconclusive',
                'failover-proof-report-failed: no peer reports '
                'role=active' if reachable
                else 'the pair is unreachable')
        if owner != 'active':
            if wait_for(lambda: _tracking_standby(ctx, 'active'),
                        time.monotonic() + PROOF_SETTLE,
                        interval=PROOF_POLL) is None:
                return case.finish('inconclusive', 'the duty peer '
                                   'is not a tracking standby — '
                                   'the launch-layout restore has '
                                   'no promotable peer')
            status, body = _settle_call(base_b + '/demote')
            if status != 200:
                return case.finish('failed', 'the entry demote '
                                   'on the armed peer answered '
                                   + str(status) + ': '
                                   + json.dumps(body)[:200])
            deadline = time.monotonic() + PROOF_SETTLE
            promoted = None
            while time.monotonic() < deadline and promoted is None:
                status, body = _settle_call(base_a + '/promote')
                if status == 200:
                    promoted = body
                elif status == 409:
                    time.sleep(PROOF_POLL)
                else:
                    return case.finish(
                        'failed', 'the entry promote on the duty '
                        'peer answered ' + str(status) + ': '
                        + json.dumps(body)[:200])
            if promoted is None:
                return case.finish('failed', 'the entry promote '
                                   'on the duty peer never '
                                   'succeeded')
        if wait_for(lambda: _tracking_standby(ctx, 'standby'),
                    time.monotonic() + PROOF_SETTLE,
                    interval=PROOF_POLL) is None:
            return case.finish('failed', 'the armed peer never '
                               'settled to a tracking standby — '
                               'the induction has no observation '
                               'point')
        case.observe('armed standby ' + base_b + ' tracking under '
                     'the declared budget ' + str(budget)
                     + '; duty peer ' + base_a)

        digests = []
        for number in (1, 2):
            digest, violations, evidence = _failover_proof_pass(
                ctx, base_a, base_b, journal_b, budget)
            ref = save_evidence(
                ctx['evidence_dir'],
                'failover-proof-report-pass-' + str(number)
                + '.json', evidence)
            case.evidence('file', ref, 'failover-proof-report '
                          'pass ' + str(number) + ' — the armed '
                          'baseline, the unarmed report, the '
                          'pre-field reconstruction, the degraded '
                          'window, the boundary fire, the '
                          'journaled record, and the normalized '
                          'digest')
            case.observe('pass ' + str(number) + ': '
                         + json.dumps(digest, sort_keys=True))
            if violations:
                has_failed = any(
                    name == 'failover-proof-report-failed'
                    for name, _ in violations.values())
                diagnostic = 'failover-proof-report-failed' \
                    if has_failed \
                    else 'failover-proof-report-nondeterministic'
                return case.finish(
                    'failed', diagnostic + ': ' + '; '.join(
                        detail for _, detail in
                        list(violations.values())[:4]))
            digests.append(digest)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', 'failover-proof-report-nondeterministic'
                ': the two passes\' digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two partition passes, identical digests — '
                     'each observed the served k-of-N accounting '
                     'beside the standing proof through the '
                     'degraded window, the armed gate\'s '
                     'self-promotion at the budget-th miss, and '
                     'the failover-origin record')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
