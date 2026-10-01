"""The keyed_announced_source leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the leg drives only the lane-staged keyed probe pair —
# its own plant, monitors, and forge — and restores the probe pair's
# launch roles every pass, so it needs only to sit behind the leg
# whose announced-source window the probe pair's keyed coverage
# extends.
RUNS_AFTER = frozenset({'scenario_demote_forged_standby_source'})


# --------------------------------------------------------------------
# The keyed announced-source lifecycle residual (the accepted QA
# finding `keyed-announced-source-untested`'s unexercised classes —
# the per-run lane evidence for the keyed tracking-source
# authenticity contract WW-FND-004/WW-LCM-001 record). Scenario
# 2050's keyed halves already cover the forged receipt-window and
# planted-internal documents and the honest keyed adoption on the
# demote-verify path; this leg stages the four classes that remained:
#
#   * forged keyed endpoints on the announced verify path — the
#     keyed forge serves a genuinely signed document whose receipt
#     window forks the owner's settled log; the verify pull must
#     meet it signed and still refuse `no_tracking_source`, with no
#     adoption or role change journaled;
#   * replay of a captured `line_proof` after its nonce window
#     rolls — a checkpoint the keyed peer itself signed under the
#     leg's own capture nonce is staged verbatim; the verify pull
#     carries a fresh nonce, so the replayed proof no longer binds
#     and the document refuses the same named refusal even though
#     every document check would pass;
#   * involuntary demotion while keying is enabled — a misordered
#     `POST /promote` on the tracking standby preempts the probe
#     pair's field claim; the superseded owner demotes in place,
#     journals the attributed FieldClaimLost, resolves a verified
#     source (the claim-declared monitor or the announced hint —
#     keyed-only paths), journals TrackingSourceAdopted naming the
#     promoted peer, and re-joins tracking inside the bound;
#   * the complete honest keyed announce-verify-pull lifecycle —
#     the keyed forge announces, the demote verify pulls it signed,
#     the adoption journals, the demoted peer's standing pulls
#     converge it `orphaned`, and when the adopted endpoint turns
#     hostile — the signed document now plants an internal `In`
#     sample no settled verdict produced — the standing pull path
#     convicts it (`degraded`, the planted value never applied)
#     before the honest document restores convergence and the peer
#     re-promotes.
#
# The subject is always the lane-staged keyed probe pair — the issue
# pins the residual exercise to it, so an unkeyed deployed pair is
# never the subject and ctx['probe']'s absence is the leg's only
# inconclusive posture. The probe pair's bridge-placed plant is not
# host-reachable, so the involuntary leg reads its fencing outcome
# from the journaled FieldClaimLost attribution rather than the raw
# verdict the deployed-pair leg probes. Named diagnostics are
# keyed-announced-source-failed for a contract miss,
# keyed-announced-source-nondeterministic when two passes disagree,
# and keyed-announced-source-unchecked when the self-check's planted
# negatives slip past the leg's own audits. Two consecutive passes
# produce identical digests.

KEYED_SETTLE = 45      # bound on each restart/switch/rejoin/reconverge
KEYED_POLL = 0.4       # wait cadence inside the leg
ANNOUNCE_SETTLE = 20   # bound on the forge's announce landing
REJOIN_TICKS = 60      # the lane tick bound on the involuntary
                       # demote's re-join — the stranded wedge the
                       # contract names
PAIR_PORTS = {'active': 8080, 'standby': 8081}
FORGE_PORT = 8090      # the bridge-placed forge's monitor port
PROOF_NONCE = 0x6b6579656450726f    # the leg's own capture nonce —
                                    # the replayed document's proof
                                    # stays bound to it while every
                                    # verify pull mints a fresh one
DIAG_FAILED = 'keyed-announced-source-failed'
DIAG_NONDET = 'keyed-announced-source-nondeterministic'
DIAG_UNCHECKED = 'keyed-announced-source-unchecked'


# ---- the forge endpoint's staged document and hits ledger ---------

def _forge_hits(path):
    """The parsed records of the forge endpoint's hits ledger —
    `{'kind': 'serve'|'announce', ...}` JSONL lines the bind-mounted
    file carries. A missing file or a torn final line reads as absent
    records — a lost observation, never the leg's verdict."""
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _forge_announced(forge):
    """True once the forge's hits ledger holds an announce pull the
    owner answered — the recorded hint POST /demote then verifies."""
    return any(record.get('kind') == 'announce' and record.get('ok')
               for record in _forge_hits(forge['hits']))


def _stage_document(forge, document):
    """Rewrite the forge's staged checkpoint document — the atomic
    rename lands the next shape without a relaunch or a torn read."""
    staged = Path(forge['dir']) / 'checkpoint.staging.json'
    staged.write_text(json.dumps(document))
    staged.replace(forge['document'])


def _standby_document(own):
    """The honest standby-shaped document derived from the field
    owner's served checkpoint: the same line's continuation stamped
    `source_owns_field: false` — the shape a real tracking peer's
    checkpoint carries — with the owner-hint and proof decorations
    stripped."""
    document = json.loads(json.dumps(own))
    document['source_owns_field'] = False
    document.pop('line_owner', None)
    document.pop('line_proof', None)
    return document


def _forked_document(own, index, point, value):
    """The receipt-window forgery: the honest standby document whose
    settled log claims a different command at this pass's settled
    submission index — the write's value flipped. Returns None when
    the owner's checkpoint no longer carries the index the forgery
    needs."""
    document = _standby_document(own)
    receipts = document.get('receipts')
    attempts = (document.get('command_admission') or {}).get('attempts')
    if not isinstance(receipts, list) \
            or not isinstance(attempts, int) \
            or isinstance(attempts, bool):
        return None
    position = index - (attempts - len(receipts))
    if not 0 <= position < len(receipts):
        return None
    forged = json.loads(json.dumps(receipts[position]))
    forged['command'] = {'write_value': {
        'point': point, 'kind': 'bool', 'value': {'bool': value}}}
    receipts[position] = forged
    return document


def _planted_document(own, point):
    """The planted-internal forgery: the honest standby document whose
    internal `In` image claims a held value no settled verdict
    produced — the target point's sample flipped from what the
    owner's own image and receipted write hold. Returns None when the
    checkpoint serves no bool sample at the point."""
    document = _standby_document(own)
    sample = (document.get('internal') or {}).get(str(point))
    if not isinstance(sample, dict):
        return None
    value = sample.get('value')
    if not isinstance(value, dict) \
            or not isinstance(value.get('bool'), bool):
        return None
    sample['value'] = {'bool': not value['bool']}
    return document


# ---- the durable journal reads -------------------------------------

def _journal_floor(ctx, name):
    """The durable journal's length — every record after this floor
    is the episode's own."""
    try:
        return len(_journal_entries(ctx['journal_files'][name]))
    except OSError:
        return 0


def _journaled(ctx, name, floor, kind):
    """The bodies of `kind` events the named peer's durable journal
    carries since `floor`, or None when the file can't be read."""
    try:
        entries = _journal_entries(ctx['journal_files'][name])
    except OSError:
        return None
    return [(item.get('entry') or {}).get('event', {})[kind]
            for item in entries[floor:]
            if kind in ((item.get('entry') or {}).get('event') or {})]


def _switch_journaled(ctx, name, floor):
    """The named peer's journal tail since `floor` as (tracking-source
    adoptions, role changes) — the durable record a refused demotion
    must not grow."""
    adoptions = _journaled(ctx, name, floor,
                           'tracking_source_adopted') or []
    changes = _journaled(ctx, name, floor, 'role_changed') or []
    return adoptions, changes


def _durable_adoptions(ctx, name):
    """Every source the named peer's durable journal's
    tracking_source_adopted records name, or None unreadable — a
    process-lifetime adoption pin from an earlier demotion leaves its
    record here even when the fresh demotion journals none."""
    try:
        records = _journal_entries(ctx['journal_files'][name])
    except OSError:
        return None
    return [(record.get('entry') or {}).get('event', {})
            .get('tracking_source_adopted', {}).get('source')
            for record in records
            if 'tracking_source_adopted'
            in ((record.get('entry') or {}).get('event') or {})]


# ---- the role/sync watches -----------------------------------------

def _sync_kind(report):
    """The sync vocabulary a standby /role report carries."""
    sync = ((report or {}).get('sync') or {})
    for kind in ('tracking', 'orphaned', 'reinitialized', 'diverged',
                 'unsynchronized'):
        if kind in sync:
            return kind
    if isinstance(sync, str):
        return sync
    return 'missing'


def _wait_following(ctx, name, watch):
    """Poll /role until the peer reports a converged standby; the
    poll rows land in `watch` for the episode evidence."""
    def found():
        report = _try_role(ctx, ctx[name])
        watch.append({name: report})
        return report if report is not None \
            and _sync_kind(report) in ('tracking', 'orphaned',
                                       'reinitialized') \
            and report.get('role') == 'standby' else None
    return wait_for(found, time.monotonic() + KEYED_SETTLE,
                    interval=KEYED_POLL)


def _wait_standby(ctx, name, watch):
    """Poll /role until the peer reports standby; the poll rows land
    in `watch` for the episode evidence."""
    def found():
        report = _try_role(ctx, ctx[name])
        watch.append({name: report})
        return report if (report or {}).get('role') == 'standby' \
            else None
    return wait_for(found, time.monotonic() + KEYED_SETTLE,
                    interval=KEYED_POLL)


def _degraded(report):
    return (report or {}).get('role') == 'standby' \
        and 'degraded' in ((report or {}).get('sync') or {})


# ---- the leg audits ------------------------------------------------
# Each leg collects its observation record and a pure judge audits
# it; the self-check replays each judge over planted negatives.

def _judge_involuntary(record, port, claimant, note):
    """Audit the involuntary-demote record: the misordered promote
    granted, the superseded owner walked to standby in place, the
    FieldClaimLost journaled attributed to the promoted peer, exactly
    one TrackingSourceAdopted naming the promoted peer's monitor —
    the keyed verified-source resolution — the rejoin landing
    tracking inside the bound, and the adopted line document naming
    the promoted peer as owner."""
    promote = record.get('promote') or {}
    if promote.get('status') != 200:
        note('the misordered POST /promote on the tracking standby '
             'answered ' + str(promote.get('status')) + ' '
             + json.dumps(promote.get('body'))[:200])
    if not record.get('demoted'):
        note('the superseded owner never walked to standby on its '
             'own fenced write — the demote-in-place did not land')
    losses = record.get('field_claim_lost')
    if not losses:
        note('the demoted peer journaled no field_claim_lost during '
             'the supersede')
    elif len(losses) != 1 \
            or (losses[0] or {}).get('claimant') != claimant:
        note('the journaled field_claim_lost is not the single '
             'attributed record naming the promoted peer\'s token: '
             + json.dumps(losses)[:200])
    rejoin_ticks = record.get('rejoin_ticks')
    if not record.get('rejoined'):
        note('the demoted peer never re-joined tracking — the '
             'stranded wedge under keying')
    elif rejoin_ticks is not None and rejoin_ticks > REJOIN_TICKS:
        note('the demoted peer took ' + str(rejoin_ticks)
             + ' ticks to re-join — beyond the lane bound '
             + str(REJOIN_TICKS))
    adoptions = record.get('tracking_source_adopted')
    if not adoptions:
        if record.get('pinned_source'):
            # A repeat demotion converges through the process-lifetime
            # pin an earlier adoption left — the verified source is
            # already durably recorded.
            pass
        else:
            note('the demoted peer journaled no '
                 'tracking_source_adopted — no verified announced or '
                 'claim-declared source resolved under keying')
    elif len(adoptions) != 1 \
            or not str((adoptions[0] or {}).get('source')) \
            .endswith(':' + str(port)):
        note('the journaled tracking_source_adopted is not the '
             'single record naming the promoted peer on :'
             + str(port) + ': ' + json.dumps(adoptions)[:200])
    document = record.get('adopted_document')
    if not isinstance(document, dict):
        note('the re-joined peer serves no adopted checkpoint '
             'document')
    elif document.get('source_owns_field') is not True \
            or not str(document.get('line_owner')) \
            .endswith(':' + str(port)):
        note('the adopted line document does not name the promoted '
             'peer\'s line as field owner on :' + str(port) + ': '
             + json.dumps(document)[:200])


def _judge_refusal(record, expect_signed, note):
    """Audit one verify-refusal record: POST /demote settles
    `409 no_tracking_source`, the journal since the leg's floor
    holds no tracking-source adoption and no role change, the peer
    still reports field owner, and the forge's hits ledger proves
    the verify pull reached it — signed exactly when `expect_signed`
    says the endpoint held the pair's token."""
    demote = record.get('demote') or {}
    if demote.get('status') != 409 \
            or demote.get('body') != 'no_tracking_source':
        note('POST /demote answered ' + str(demote.get('status'))
             + ' ' + json.dumps(demote.get('body'))[:200]
             + ' instead of settling no_tracking_source')
    if record.get('journaled_adoptions'):
        note('the refused demote journaled a tracking-source '
             'adoption: '
             + json.dumps(record['journaled_adoptions'])[:200])
    if record.get('journaled_role_changes'):
        note('the refused demote journaled a role change: '
             + json.dumps(record['journaled_role_changes'])[:200])
    if ((record.get('role_after') or {}).get('role')) != 'active':
        note('the peer stopped owning the field on the refused '
             'demote: ' + json.dumps(record.get('role_after'))[:200])
    pulls = record.get('verify_pulls') or []
    if not pulls:
        note('the forge ledgered no checkpoint pull — the refusal '
             'never reached the staged document')
    elif expect_signed \
            and not any(pull.get('signed') for pull in pulls):
        note('no verify pull was answered signed — the keyed '
             'document never reached the audit')
    elif not expect_signed \
            and any(pull.get('signed') for pull in pulls):
        note('the unsigned endpoint answered signed — the replay '
             'leg never exercised the rolled nonce')


def _judge_replay(record, note):
    """Audit the stale-proof replay record on top of the refusal
    checks: the staged document carried a captured line_proof and
    every verify pull arrived under a nonce the captured proof was
    never bound to — the replay, not a missing proof, is what the
    refusal names."""
    _judge_refusal(record, False, note)
    staged = record.get('staged_document')
    if not isinstance(staged, dict) \
            or staged.get('line_proof') is None:
        note('the staged document carried no captured line_proof — '
             'the leg replayed nothing')
    served_nonces = [((pull.get('query') or '').partition('prove=')[2]
                      or None)
                     for pull in (record.get('verify_pulls') or [])]
    if staged is not None and served_nonces \
            and str(PROOF_NONCE) in served_nonces:
        note('the verify pull carried the capture nonce — the '
             'nonce window never rolled, so the replay tested '
             'nothing')


def _judge_lifecycle(record, forge_port, note):
    """Audit the honest announce-verify-pull record: POST /demote
    settles 200, exactly one TrackingSourceAdopted names the forge's
    port, the demoted peer reconverges a following standby, and the
    re-promote returns it to active."""
    demote = record.get('demote') or {}
    if demote.get('status') != 200:
        note('the honest announced document was refused — the '
             'legitimate follow-peer path is closed: '
             + str(demote.get('status')) + ' '
             + json.dumps(demote.get('body'))[:200])
    adoptions = record.get('journaled_adoptions') or []
    if len(adoptions) != 1 \
            or not str((adoptions[0] or {}).get('source')) \
            .endswith(':' + str(forge_port)):
        note('the honest demote journaled '
             + json.dumps(adoptions)[:200]
             + ' instead of one tracking-source adoption naming '
             'the forge on :' + str(forge_port))
    if not record.get('reconverged'):
        note('the demoted peer never reconverged onto the adopted '
             'announced source')
    if not record.get('promoted'):
        note('the reconverged peer never re-promoted to active')


def _judge_pull(record, held, note):
    """Audit the hostile-pull record: the adopted endpoint now
    serving the signed planted document leaves the demoted peer
    degraded — the standing pull path's named refusal — with the
    planted value never applied to its image and no new adoption or
    role change journaled; the honest document restores
    convergence."""
    if not record.get('degraded'):
        note('the demoted peer never reported the degraded sync '
             'while its adopted source served the planted document '
             '— the pull-path audit convicted nothing')
    moved = [sample for sample in (record.get('samples') or [])
             if sample is not None and sample != held]
    if held is not None and moved:
        note('the planted internal value landed on the demoted '
             'peer\'s image: ' + json.dumps(moved)[:200])
    if record.get('journaled_adoptions'):
        note('the hostile pull journaled a tracking-source '
             'adoption: '
             + json.dumps(record['journaled_adoptions'])[:200])
    if record.get('journaled_role_changes'):
        note('the hostile pull journaled a role change: '
             + json.dumps(record['journaled_role_changes'])[:200])
    if not record.get('recovered'):
        note('the demoted peer never reconverged once the staged '
             'document went honest again')


def _self_check():
    """The leg's unchecked-diagnostic self-test: run every judge
    over the planted negative it must name — a refusal record
    claiming the forged endpoint adopted, a replay record missing
    its staged proof, an involuntary record whose adoption names
    the wrong endpoint, a lifecycle record missing its converge,
    a pull record claiming the planted value landed — and require
    the judge to note the violation. A silent judge returns the
    negative names it let through."""
    slipped = []

    def expect(name, judge):
        found = []
        judge(lambda detail: found.append(detail))
        if not found:
            slipped.append(name)

    # The forged keyed endpoint adopted — every refusal clause
    # tripped at once.
    expect('forged-verify-accepted', lambda note: _judge_refusal({
        'demote': {'status': 200, 'body': {'role': 'demoting'}},
        'verify_pulls': [{'kind': 'serve', 'signed': True}],
        'journaled_adoptions': [{'source': 'probe-forge:8090'}],
        'journaled_role_changes': [{'from': 'active',
                                    'to': 'demoting'}],
        'role_after': {'role': 'standby'}}, True, note))
    # The captured proof verified — the replay the leg names.
    expect('stale-proof-verified', lambda note: _judge_replay({
        'demote': {'status': 200, 'body': {'role': 'demoting'}},
        'verify_pulls': [{'kind': 'serve', 'signed': False,
                          'query': 'prove=999'}],
        'staged_document': {'line_proof': 12345},
        'journaled_adoptions': [], 'journaled_role_changes': [],
        'role_after': {'role': 'active'}}, note))
    # The involuntary demote resolved onto the wrong source — the
    # verified-adoption evidence absent.
    expect('involuntary-misresolved', lambda note: _judge_involuntary(
        {'promote': {'status': 200, 'body': {'role': 'promoting'}},
         'demoted': {'role': 'standby'},
         'field_claim_lost': [{'claimant': 424249}],
         'tracking_source_adopted': [{'source': 'probe-forge:8090'}],
         'rejoined': {'role': 'standby',
                      'sync': {'tracking': {'aligned': 1}}},
         'rejoin_ticks': 3,
         'adopted_document': {'source_owns_field': True,
                              'line_owner': 'probe-b:8081'}},
        PAIR_PORTS['standby'], 424249, note))
    # The honest lifecycle adopted but never converged.
    expect('lifecycle-stranded', lambda note: _judge_lifecycle({
        'demote': {'status': 200, 'body': {'role': 'demoting'}},
        'journaled_adoptions': [{'source': 'probe-forge:8090'}],
        'reconverged': None, 'promoted': None}, FORGE_PORT, note))
    # The planted document applied on the standing pull path.
    expect('pull-applied', lambda note: _judge_pull({
        'degraded': {'role': 'standby',
                     'sync': {'degraded': {'detail': 'x'}}},
        'samples': [True, False], 'journaled_adoptions': [],
        'journaled_role_changes': [], 'recovered': {'role': 'standby'}},
        True, note))
    return slipped


# ---- the episode legs ----------------------------------------------

def _restore_layout(ctx, owner, peer):
    """Best-effort launch-layout restore on the probe pair: demote
    whichever peer still owns the field onto its configured source,
    promote the launch owner, and let the pair reconverge."""
    try:
        report = _try_role(ctx, ctx[peer])
        if (report or {}).get('role') in ('active', 'promoting'):
            _settle_call(ctx[peer] + '/demote')
            _wait_standby(ctx, peer, [])
        report = _try_role(ctx, ctx[owner])
        if (report or {}).get('role') != 'active':
            _settle_call(ctx[owner] + '/promote')
        wait_for(lambda: (_try_role(ctx, ctx[owner]) or {})
                 .get('role') == 'active' or None,
                 time.monotonic() + KEYED_SETTLE, interval=KEYED_POLL)
        wait_for(lambda: _tracking_standby(ctx, peer) or None,
                 time.monotonic() + KEYED_SETTLE, interval=KEYED_POLL)
    except Exception:
        pass


def _involuntary_leg(ctx, demoted, promoted, floor):
    """The involuntary-demote leg: POST /promote on the tracking
    standby `promoted` while `demoted` still owns the probe pair's
    field — never POST /demote first. The promoted peer preempts the
    claim; the superseded owner's first fenced write demotes it in
    place; under keying its tracking path must resolve a verified
    source — the standing claim's declared monitor or the recorded
    announce — journal the attributed FieldClaimLost and the
    TrackingSourceAdopted naming the promoted peer, and re-join
    tracking inside the bound. Returns the record the judge audits."""
    record = {'demoted': None, 'promoted_key': promoted}
    port = PAIR_PORTS[promoted]
    status, body = _settle_call(ctx[promoted] + '/promote')
    record['promote'] = {'status': status, 'body': body}
    if status != 200:
        return record

    # The demote-in-place watch: the superseded owner must walk to
    # standby on its own fenced write — never disappear, never hold
    # 'active' alongside the promoted peer.
    walk = []
    walked = _wait_standby(ctx, demoted, walk)
    record['demotion_watch'] = walk[-8:]
    record['demoted'] = walked
    if walked is None:
        return record
    demote_tick = walked.get('tick') \
        if isinstance(walked.get('tick'), int) else None

    # The re-join watch: under keying the demoted peer resolves the
    # verified source and reports tracking inside the lane's tick
    # bound.
    rejoin = []
    tracked = None
    deadline = time.monotonic() + KEYED_SETTLE
    while tracked is None and time.monotonic() < deadline:
        report = _try_role(ctx, ctx[demoted])
        rejoin.append({demoted: report})
        if (report or {}).get('role') == 'standby' \
                and _sync_kind(report) == 'tracking':
            tracked = report
        else:
            time.sleep(KEYED_POLL)
    record['rejoin_watch'] = rejoin[-8:]
    record['rejoined'] = tracked
    if tracked is not None:
        track_tick = tracked.get('tick')
        if isinstance(track_tick, int) and demote_tick is not None:
            record['rejoin_ticks'] = track_tick - demote_tick

    # The journaled evidence on the demoted peer: the attributed
    # FieldClaimLost the fencing write owes, and the single
    # TrackingSourceAdopted naming the promoted peer's monitor — the
    # verified-source resolution the keyed contract requires.
    record['field_claim_lost'] = _journaled(
        ctx, demoted, floor, 'field_claim_lost')
    record['tracking_source_adopted'] = _journaled(
        ctx, demoted, floor, 'tracking_source_adopted')
    record['durable_sources'] = _durable_adoptions(ctx, demoted)
    record['pinned_source'] = any(
        str(source).endswith(':' + str(port))
        for source in (record['durable_sources'] or []))

    # The adopted line document the re-joined peer now serves.
    try:
        _, doc = http_json('GET', ctx[demoted] + '/checkpoint')
        record['adopted_document'] = {
            'source_owns_field': doc.get('source_owns_field'),
            'line_owner': doc.get('line_owner'),
            'generation': doc.get('generation')}
    except Exception:
        record['adopted_document'] = None
    record['settled_peer'] = _try_role(ctx, ctx[promoted])
    return record


def _refusal_leg(ctx, base, owner, floor, forge):
    """One verify-refusal leg: POST /demote on the field owner while
    the forge serves its staged document, collecting the record
    `_judge_refusal` audits — the demote answer, the verify pulls
    the hits ledger holds, and the journaled adoption/role tail that
    must stay empty."""
    hits_before = len(_forge_hits(forge['hits']))
    status, body = _settle_call(base + '/demote')
    pulls = [record for record in _forge_hits(forge['hits'])[hits_before:]
             if record.get('kind') == 'serve']
    role = _try_role(ctx, base)
    adoptions, changes = _switch_journaled(ctx, owner, floor)
    return {'demote': {'status': status, 'body': body},
            'verify_pulls': pulls, 'role_after': role,
            'journaled_adoptions': adoptions,
            'journaled_role_changes': changes}


def _keyed_pass(ctx, number, owner, peer, point, baseline, tokens):
    """One keyed-lifecycle pass on the probe pair: the involuntary
    demote under keying, then the announced-only window carrying the
    forged keyed verify refusal, the stale line_proof replay, the
    honest announce-verify-pull lifecycle, and the hostile pull —
    the launch roles restored. Returns (digest, violations,
    evidence): digest is the pass's normalized verdict record,
    identical across clean passes; violations maps each failing key
    to (diagnostic, detail)."""
    violations = {}
    evidence = {'entry_owner': owner, 'pass': number}
    digest = {'involuntary': 'unresolved', 'forged_verify': 'adopted',
              'stale_proof': 'verified', 'lifecycle': 'incomplete',
              'forged_pull': 'applied', 'roles': 'unrestored'}

    def note(key, diagnostic, detail):
        violations.setdefault(key, (diagnostic, detail))

    def failed(key, detail):
        note(key, DIAG_FAILED, detail)

    base, peer_base = ctx[owner], ctx[peer]
    evidence.update({'owner': owner, 'peer': peer})
    legs = {}
    evidence['legs'] = legs

    # The settle gate: the probe pair's launch layout — the
    # unconfigured owner holds the field, the configured standby
    # tracks it.
    if _pair_active(ctx) != owner \
            or _tracking_standby(ctx, peer) is None:
        failed('settle', 'the probe pair never settled — ' + owner
               + ' holds no active role with ' + peer
               + ' tracking behind it')
        return None, violations, evidence

    # The pass's settled command: the receipt the fork forges
    # against and the held value the planted document contradicts.
    index = _next_receipt_index(ctx, base)
    command = {'command': {'write_value': {
        'point': point, 'kind': 'bool', 'value': {'bool': not baseline}}},
        'actor': 'qa-lane-keyed-announced-' + str(number)}
    try:
        command_status, receipt = http_json(
            'POST', base + '/command', command)
    except Exception as exc:
        failed('command', 'the field owner never answered the pass '
               'command: ' + str(exc)[:200])
        return None, violations, evidence
    settled = wait_for(
        lambda: _settled_outcome(ctx, base, index),
        time.monotonic() + KEYED_SETTLE, interval=KEYED_POLL)
    evidence['command'] = {'index': index, 'status': command_status,
                           'receipt': receipt, 'settled': settled}
    if settled != 'applied':
        failed('command', 'the pass command never settled applied — '
               'the forgeries have no settled receipt or held value '
               'to contradict: ' + json.dumps(receipt)[:200])
        return None, violations, evidence

    # The captured keyed document: the tracking peer's own
    # `?prove=` answer — signed under the leg's nonce, staged later
    # verbatim so its proof meets only the rolled nonce window.
    try:
        _, captured = http_json(
            'GET', peer_base + '/checkpoint?prove='
            + str(PROOF_NONCE))
    except Exception as exc:
        evidence['capture'] = str(exc)
        failed('capture', 'the tracking peer never answered the '
               'proof-bearing checkpoint pull: ' + str(exc)[:200])
        return None, violations, evidence
    evidence['capture'] = {'nonce': PROOF_NONCE,
                           'generation': captured.get('generation'),
                           'tick': captured.get('tick'),
                           'signed': captured.get('line_proof')
                           is not None}
    if not isinstance(captured, dict) \
            or captured.get('line_proof') is None \
            or captured.get('generation') is None:
        evidence['inconclusive'] = \
            'the keyed peer\'s ?prove= answer carries no line_proof '\
            '— the capture lever the replay leg needs is absent'
        return None, violations, evidence

    # The involuntary leg: the misordered promote preempts the probe
    # pair's field claim; the launch owner — the peer with no
    # configured source — demotes in place and must resolve a
    # verified source under keying. The launch layout restores so
    # the announced window opens on the same entry shape.
    floor = _journal_floor(ctx, owner)
    record = _involuntary_leg(ctx, owner, peer, floor)
    legs['involuntary'] = record
    problems = []
    _judge_involuntary(record, PAIR_PORTS[peer],
                       tokens.get(peer), problems.append)
    if not problems:
        digest['involuntary'] = 'resolved'
    for detail in problems:
        failed('involuntary', detail)
    _restore_layout(ctx, owner, peer)
    restored = wait_for(
        lambda: (_pair_active(ctx) == owner or None)
        and _tracking_standby(ctx, peer),
        time.monotonic() + KEYED_SETTLE, interval=KEYED_POLL)
    legs['involuntary']['restored'] = restored is not None
    if restored is None:
        failed('involuntary-restore', 'the launch layout never '
               'restored after the involuntary-demote cycle')
        return None, violations, evidence

    # The announced-only window: the tracking peer stopped, the
    # owner warm-restarted so its announced set begins empty — the
    # forge's announce below is then the only hint the demote
    # verifies. The warm restart keeps the checkpoint generation,
    # so the captured document still claims this line — its proof
    # is the only thing the verify can convict.
    try:
        ctx['stop_controller'](peer)
        ctx['restart_controller'](owner)
    except Exception as exc:
        failed('window', 'the lifecycle actions never opened the '
               'announced-only window: ' + str(exc)[:200])
        return None, violations, evidence
    restored = wait_for(
        lambda: ((_try_role(ctx, base) or {}).get('role') == 'active'
                 and _try_role(ctx, base)) or None,
        time.monotonic() + KEYED_SETTLE, interval=KEYED_POLL)
    evidence['window'] = {'peer': 'stopped',
                          'owner_restart': restored}
    if restored is None:
        failed('window', 'the restarted owner never reported '
               'active again')
        return None, violations, evidence
    floor = _journal_floor(ctx, owner)
    try:
        _, own = http_json('GET', base + '/checkpoint')
    except Exception as exc:
        failed('checkpoint', 'the restarted owner\'s checkpoint '
               'never answered: ' + str(exc)[:200])
        return None, violations, evidence
    if not isinstance(own, dict) \
            or own.get('source_owns_field') is not True \
            or not isinstance(own.get('tick'), int):
        failed('checkpoint', 'the restarted owner serves no '
               'field-owning checkpoint document: '
               + json.dumps(own)[:200])
        return None, violations, evidence
    if captured.get('generation') is not None \
            and captured.get('generation') != own.get('generation'):
        # The captured document's line ended — the replay staged
        # below would refuse the generation, not the proof.
        evidence['capture']['stale'] = True
        failed('capture', 'the captured document\'s generation '
               'does not survive the owner\'s warm restart — the '
               'replay would convict the stream, not the proof')
        return None, violations, evidence

    honest = _standby_document(own)
    forked = _forked_document(own, index, point, baseline)
    planted = _planted_document(own, point)
    evidence['documents'] = {
        'honest': 'standby-shaped continuation',
        'receipt_fork': None if forked is None else
        'receipts[%d].command claims the flipped write' % index,
        'planted_internal': None if planted is None else
        'internal[%d] plants %s' % (point, json.dumps(
            (planted['internal'][str(point)] or {}).get('value'))),
        'stale_proof': 'peer document signed under nonce '
        + str(PROOF_NONCE)}
    if forked is None or planted is None:
        failed('documents', 'the served checkpoint cannot stage '
               'the forgeries: '
               + json.dumps(evidence['documents'])[:300])
        return None, violations, evidence

    forge = None
    try:
        # The forged keyed verify leg: the token-holding endpoint
        # signs every answer, so only the staged document's content
        # can convict it — the receipt fork refuses named.
        forge = ctx['start_forge'](forked, owner, keyed=True)
        if wait_for(lambda: _forge_announced(forge) or None,
                    time.monotonic() + ANNOUNCE_SETTLE,
                    interval=KEYED_POLL) is None:
            failed('forge-announce', 'the keyed forge\'s announce '
                   'never landed on the owner — its hint was never '
                   'recorded')
            return None, violations, evidence
        record = _refusal_leg(ctx, base, owner, floor, forge)
        legs['forged_verify'] = record
        problems = []
        _judge_refusal(record, True, problems.append)
        if not problems:
            digest['forged_verify'] = 'refused'
        for detail in problems:
            failed('forged-verify', detail)
        ctx['stop_forge']()
        forge = None

        # The stale-proof replay leg: the peer's own signed
        # checkpoint — captured under the leg's nonce, before the
        # window — served verbatim by an endpoint holding no pair
        # token. The document passes every document check the
        # verify runs (this line, this generation, the standby
        # stamp); only the proof, bound to a nonce the fresh pull
        # no longer carries, refuses it.
        forge = ctx['start_forge'](captured, owner, keyed=False)
        if wait_for(lambda: _forge_announced(forge) or None,
                    time.monotonic() + ANNOUNCE_SETTLE,
                    interval=KEYED_POLL) is None:
            failed('forge-announce', 'the replay forge\'s announce '
                   'never landed on the owner — its hint was never '
                   'recorded')
            return None, violations, evidence
        record = _refusal_leg(ctx, base, owner, floor, forge)
        record['staged_document'] = {
            'line_proof': captured.get('line_proof'),
            'generation': captured.get('generation'),
            'tick': captured.get('tick'),
            'source_owns_field': captured.get('source_owns_field')}
        legs['stale_proof'] = record
        problems = []
        _judge_replay(record, problems.append)
        if not problems:
            digest['stale_proof'] = 'refused'
        for detail in problems:
            failed('stale-proof', detail)
        ctx['stop_forge']()
        forge = None

        # The honest lifecycle leg: the keyed endpoint serving the
        # standby-shaped continuation — announce, signed verify
        # pull, journaled adoption, standing pulls converging the
        # demoted peer, the hostile restage refused on the pull
        # path, and the re-promote closing the lifecycle.
        forge = ctx['start_forge'](honest, owner, keyed=True)
        if wait_for(lambda: _forge_announced(forge) or None,
                    time.monotonic() + ANNOUNCE_SETTLE,
                    interval=KEYED_POLL) is None:
            failed('forge-announce', 'the honest forge\'s announce '
                   'never landed on the owner — its hint was never '
                   'recorded')
            return None, violations, evidence
        hits_before = len(_forge_hits(forge['hits']))
        demote_status, demote = _settle_call(base + '/demote')
        record = {'demote': {'status': demote_status,
                             'body': demote},
                  'verify_pulls': [
                      hit for hit in
                      _forge_hits(forge['hits'])[hits_before:]
                      if hit.get('kind') == 'serve'],
                  'role_after': _try_role(ctx, base)}
        legs['lifecycle'] = record
        adoptions, changes = _switch_journaled(ctx, owner, floor)
        record['journaled_adoptions'] = adoptions
        record['journaled_role_changes'] = changes
        record['reconverged'] = None
        record['promoted'] = None
        if demote_status == 200 \
                and len(adoptions) == 1 \
                and str((adoptions[0] or {}).get('source')) \
                .endswith(':' + str(forge['port'])):
            watch = []
            record['reconverged'] = _wait_following(ctx, owner,
                                                    watch)
            record['converge_watch'] = watch[-8:]

            # The hostile restage: the adopted endpoint now serves
            # the signed planted document — the standing pull path
            # must convict it `degraded`, never apply the planted
            # internal value, and journal no new switch; the honest
            # restage restores convergence.
            pull_floor = _journal_floor(ctx, owner)
            held = _point_value(_snapshot(ctx, base), point)
            _stage_document(forge, planted)
            pull = {'degraded': None, 'samples': [],
                    'journaled_adoptions': None,
                    'journaled_role_changes': None,
                    'recovered': None}
            legs['forged_pull'] = pull
            watch = []
            deadline = time.monotonic() + KEYED_SETTLE
            while time.monotonic() < deadline:
                report = _try_role(ctx, base)
                watch.append({owner: report})
                try:
                    pull['samples'].append(
                        _point_value(_snapshot(ctx, base), point))
                except Exception:
                    pull['samples'].append(None)
                if _degraded(report):
                    pull['degraded'] = report
                    break
                time.sleep(KEYED_POLL)
            pull['degrade_watch'] = watch[-8:]
            pull_adoptions, pull_changes = _switch_journaled(
                ctx, owner, pull_floor)
            pull['journaled_adoptions'] = pull_adoptions
            pull['journaled_role_changes'] = pull_changes
            pull['staged_held'] = held
            _stage_document(forge, honest)
            watch = []
            pull['recovered'] = _wait_following(ctx, owner, watch)
            pull['recover_watch'] = watch[-8:]
            problems = []
            _judge_pull(pull, held, problems.append)
            if not problems:
                digest['forged_pull'] = 'refused'
            for detail in problems:
                failed('forged-pull', detail)

            # The lifecycle's close: the reconverged peer
            # re-promotes onto the field.
            promoted, last = None, None
            deadline = time.monotonic() + KEYED_SETTLE
            while time.monotonic() < deadline \
                    and promoted is None:
                promote_status, body = _settle_call(
                    base + '/promote')
                if promote_status == 200:
                    promoted = body
                else:
                    last = (promote_status, body)
                    time.sleep(KEYED_POLL)
            record['promote'] = {'body': promoted,
                                 'last_refusal': last}
            record['promoted'] = wait_for(
                lambda: (_pair_active(ctx) == owner or None)
                and _try_role(ctx, base),
                time.monotonic() + KEYED_SETTLE,
                interval=KEYED_POLL) if promoted is not None else None
        problems = []
        _judge_lifecycle(record, forge['port'], problems.append)
        if not problems:
            digest['lifecycle'] = 'completed'
        for detail in problems:
            failed('lifecycle', detail)
    finally:
        if forge is not None:
            try:
                ctx['stop_forge']()
            except Exception:
                pass

    # The restore: the tracking peer's container back up — its
    # configured standby pull re-announces and re-tracks the owner
    # — so the next pass and the cases behind this one meet the
    # launch layout again.
    try:
        ctx['start_controller'](peer)
    except Exception as exc:
        failed('restore', 'the stopped peer never came back: '
               + str(exc)[:200])
        return None, violations, evidence
    settled = wait_for(
        lambda: (_pair_active(ctx) == owner or None)
        and _tracking_standby(ctx, peer),
        time.monotonic() + KEYED_SETTLE, interval=KEYED_POLL)
    evidence['restored'] = settled is not None
    if settled is None:
        failed('restore', 'the pair never settled back to its '
               'entry role layout')
    else:
        digest['roles'] = 'restored'
    evidence['digest'] = dict(digest)
    evidence['violations'] = {key: diagnostic
                              for key, (diagnostic, _)
                              in violations.items()}
    return digest, violations, evidence


def scenario_keyed_announced_source(ctx):
    """Exercise the keyed announced-source lifecycle residual on
    the run's lane-staged keyed probe pair — never the deployed
    unkeyed pair: the misordered promote's involuntary demote
    resolving a verified source, the forged keyed endpoint refused
    on the announced verify path, the captured line_proof refused
    once its nonce window rolled, and the honest
    announce-verify-pull lifecycle completing — including the
    adopted endpoint's planted document convicted on the standing
    pull path."""
    case = Case(
        'keyed-announced-source',
        'Keyed announced-source lifecycle residual on the staged '
        'probe pair',
        'on the lane-staged keyed probe pair — its own plant, '
        'monitors, and forge, never the deployed pair — the '
        'misordered POST /promote preempts the field claim: the '
        'superseded owner demotes in place, journals the '
        'attributed FieldClaimLost and the TrackingSourceAdopted '
        'naming the promoted peer, and re-joins tracking inside '
        'the bound; then, with the announced-only window open, '
        'POST /demote refuses no_tracking_source for the keyed '
        'endpoint serving the receipt-window-forked document and '
        'for the replayed checkpoint whose captured line_proof '
        'survives no nonce window, while the keyed endpoint '
        'serving the honest standby-shaped document verifies, '
        'journals its adoption, converges the demoted peer, '
        'refuses the adopted endpoint\'s planted document on the '
        'standing pull path degraded, and re-promotes; the pair '
        'restores its launch roles and two passes produce '
        'identical digests')
    try:
        # The subject is the staged probe pair alone — the issue
        # pins the residual exercise to it; its absence (or an
        # unkeyed probe staging) is capability-inconclusive, never
        # a deployment-pair fallback.
        subject = ctx.get('probe')
        if not isinstance(subject, dict) \
                or not subject.get('pair_token'):
            return case.finish('inconclusive', 'the run context '
                               'carries no keyed probe pair — the '
                               'leg\u2019s subject is the lane-staged '
                               'keyed pair, never the deployed '
                               'unkeyed pair')
        ctx = subject
        case.observe('exercised on the lane-staged keyed probe '
                     'pair — its own plant and endpoints')
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the probe subject '
                               'carries only one endpoint — the '
                               'keyed pair the leg needs is absent')
        for action in ('stop_controller', 'start_controller',
                       'restart_controller', 'start_forge',
                       'stop_forge'):
            if ctx.get(action) is None:
                return case.finish('inconclusive', 'the probe '
                                   'subject carries no ' + action
                                   + ' action — the lifecycle '
                                   'windows cannot be driven')
        placements = ctx.get('endpoint_placement') or {}
        if placements.get('forge') != 'bridge':
            return case.finish('inconclusive', 'endpoint_placement '
                               'does not place forge on the rig '
                               'bridge — a rig-dialed endpoint '
                               'cannot stand on the host')
        journal_files = ctx.get('journal_files') or {}
        if not all(journal_files.get(name)
                   for name in ('active', 'standby')):
            return case.finish('inconclusive', 'the probe subject '
                               'carries no per-controller journal '
                               'files — the journaled-evidence '
                               'audit cannot run')
        tokens = ctx.get('plant_owner') or {}
        if not all(tokens.get(name)
                   for name in ('active', 'standby')):
            return case.finish('inconclusive', 'the probe subject '
                               'records no pinned --owner-token for '
                               'the pair — the superseding claim is '
                               'not attributable')
        for name in ('active', 'standby'):
            try:
                _role(ctx, ctx[name])
            except Exception as exc:
                return case.finish('inconclusive', 'probe ' + name
                                   + '\'s monitor is unreachable: '
                                   + str(exc)[:200])

        # The launch layout the legs stage from: probe-a — the peer
        # with no configured tracking source — owns the field so
        # its involuntary demote owes the verified adoption; a
        # swapped layout is restored before the leg reports.
        deadline = time.monotonic() + KEYED_SETTLE
        if _pair_active(ctx) != 'active':
            _restore_layout(ctx, 'active', 'standby')
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=KEYED_POLL)
        if owner != 'active':
            reports = {name: _try_role(ctx, ctx[name])
                       for name in ('active', 'standby')}
            if all(report is None for report in reports.values()):
                return case.finish('inconclusive', 'the probe pair '
                                   'is unreachable — monitor '
                                   'endpoints ' + ctx['active']
                                   + ' and ' + ctx['standby'])
            return case.finish('inconclusive', 'the probe pair '
                               'never settled on its launch layout '
                               '— the unconfigured peer must hold '
                               'the field for the involuntary '
                               'adoption the leg owes')
        if wait_for(lambda: _tracking_standby(ctx, 'standby'),
                    deadline, interval=KEYED_POLL) is None:
            return case.finish('inconclusive', 'the probe pair has '
                               'no tracking standby — the settle '
                               'the leg restores to was never '
                               'reached')
        owner = 'active'
        peer = 'standby'
        case.observe('probe owner: ' + owner + ' (' + ctx[owner]
                     + '); tracking peer: ' + peer)

        _, signals = http_json('GET', ctx[owner] + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'keyed-announced-signals.json',
                            signals)
        case.evidence('file', ref, 'SignalIndex naming the settled '
                      'write the forgeries contradict')
        target = _writable_bool_point(signals)
        if target is None or target.get('point') is None:
            return case.finish('inconclusive', 'the probe model '
                               'declares no writable bool in-point '
                               'for the forgeries to contradict')
        point = target['point']
        baseline = _point_value(_snapshot(ctx, ctx[owner]), point)
        if not isinstance(baseline, bool):
            return case.finish('inconclusive', 'point ' + str(point)
                               + ' serves no bool baseline to '
                               'write against')

        # The capture lever: the keyed peer's `?prove=` answer must
        # carry a bound line_proof or the replay leg cannot stage —
        # capability-inconclusive, never a verdict.
        try:
            _, proof = http_json(
                'GET', ctx[peer] + '/checkpoint?prove='
                + str(PROOF_NONCE))
        except Exception as exc:
            return case.finish('inconclusive', 'the keyed peer '
                               'never answered the proof-bearing '
                               'checkpoint pull: ' + str(exc)[:200])
        if not isinstance(proof, dict) \
                or proof.get('line_proof') is None:
            return case.finish('inconclusive', 'the keyed peer '
                               'answers ?prove= without a '
                               'line_proof — the staged rig '
                               'predates the keyed proof contract')

        digests = []
        try:
            for number in (1, 2):
                digest, violations, evidence = _keyed_pass(
                    ctx, number, owner, peer, point, baseline,
                    tokens)
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'keyed-announced-pass-' + str(number)
                    + '.json', evidence)
                case.evidence('file', ref, 'keyed-lifecycle pass '
                              + str(number) + ' — the involuntary '
                              'demote, the announced window, the '
                              'staged documents, each leg\u2019s '
                              'record with the forge pull ledger '
                              'and journal audit, and the '
                              'normalized digest')
                if evidence.get('inconclusive'):
                    return case.finish(
                        'inconclusive', evidence['inconclusive'])
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
        finally:
            # The launch layout for the cases behind this one — a
            # clean pass restores it by construction; an aborted
            # pass gets the probe forge removed, the peer's
            # container back, and the documented role order run
            # again, best-effort.
            try:
                ctx['stop_forge']()
            except Exception:
                pass
            try:
                ctx['start_controller'](peer)
            except Exception:
                pass
            _restore_layout(ctx, owner, peer)

        if digests[0] != digests[1]:
            return case.finish(
                'failed', DIAG_NONDET + ': the two passes\' '
                'digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two keyed-lifecycle passes, identical '
                     'digests: '
                     + json.dumps(digests[0], sort_keys=True))

        # The unchecked-diagnostic self-check: every judge replays
        # the planted negative it must name; a silent judge means
        # the leg can no longer catch what it names.
        slipped = _self_check()
        if slipped:
            return case.finish('failed', DIAG_UNCHECKED
                               + ': planted negatives slipped the '
                               'leg\u2019s own audits: '
                               + ', '.join(slipped))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
