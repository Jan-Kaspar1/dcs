"""The yielded_claim_rearm acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the yielded-claim re-arm leg restores what it moves —
# the controller half's switch, yield, orphan re-promotion, and cold
# restart land back on the launch claim state and roles, and the
# socket half's same-owner re-grant re-arms in place — so it needs
# no declared window.

# --------------------------------------------------------------------
# The yielded-claim re-arm contract (WW-LCM-001's ownership-integrity
# clause — the per-revision lane evidence for #1123's fix, answering
# the finding `yielded-claim-regrant-leaves-live-incumbent-
# preemptable`): `release_writer{keep_claim}` — the demotion shape —
# leaves the standing claim yielded so a successor's conditional
# claim may preempt the deliberate hand-off 'despite other holders'.
# The yield mark is a hand-off signal, not a state: once the same
# owner re-grants itself into a live *controller* hold — an orphan
# failover's re-promotion, a bound reclaim, or a same-owner join
# through `grant_writer_claim_locked` or the `EnsureWriter` rebind —
# the mark must clear, re-arming the live-incumbent refusal
# `claim_writer_unless_held` exists to give a restarted peer's
# stale-resume claim. Before the fix the mark never cleared: every
# later conditional claimer preempted the live re-bound incumbent,
# and the observed consequence was a restarted, previously-demoted
# controller seizing the field from a live failover successor with
# no operator action.
#
# The leg stages the contract's own lifecycle on the deployed pair.
# With the pair settled and tracking, the documented switch puts the
# failover-armed peer on the field; `POST /demote` then yields its
# claim under its own pinned token — the deliberate hand-off — while
# third-party mutations stay fenced naming it. Its own tracking
# scans pull the demoted tracker's ownerless checkpoints — each
# orphaned apply a heartbeat miss — until the budget-th apply
# self-promotes it over its own yielded claim: the same-owner
# re-grant the contract says clears the mark, re-binding it as the
# live owner of its own standing claim. The demoted ex-owner then
# cold-restarts as its configured active: its startup conditional
# claim must answer fenced — the stale-resume seizure the grant
# exists to refuse — never preempting the re-bound incumbent, which
# keeps field ownership through both peers' serving monitors and
# durable journals and journals no field_claim_lost/origin=fenced
# demotion. Where the lane's claim-aware attachment admits it, the
# leg then re-stages the socket-level shape the finding records on
# the re-bound claim itself: yield it again through
# `release_writer{keep_claim}`, re-grant it same-owner through
# `claim_writer_unless_held`, and a different owner's conditional
# claim must answer fenced naming the incumbent.
#
# Two consecutive passes must produce identical digests — the named
# diagnostics are yielded-claim-rearm-failed and
# yielded-claim-rearm-nondeterministic — and the unchecked-
# diagnostic self-check replays each judge over a planted negative:
# a silent audit reports yielded-claim-rearm-unchecked. The leg is
# inconclusive when the staged run predates the contract — no claim
# lifecycle verbs, no fencing-verdict owner attribution, no failover
# budget or owner-token pins, or the pair never settles — and the
# pair's launch roles are restored for the legs behind this one.

REARM_SETTLE = 30    # bound on the pair reporting the launch layout
REARM_POLL = 0.5     # cadence polling the peers mid-episode
REARM_SWITCH = 45    # bound on the switch's reconvergence
REARM_DEMOTE = 20    # bound on a demotion settling standby
REARM_REPROMOTE = 20 # base bound the orphan failover scales
REARM_ROUNDS = 6     # polls the restartee window spans
REARM_GRACE = 2      # beat for the restartee's claim to land
REARM_RESTORE = 25   # bound on the launch-layout restore
REARM_RECLAIM = 30   # bound on the induction release's re-seat

DIAG_FAILED = 'yielded-claim-rearm-failed'
DIAG_NONDET = 'yielded-claim-rearm-nondeterministic'
DIAG_UNCHECKED = 'yielded-claim-rearm-unchecked'

# The induction token the different-owner conditional probes claim
# under — a small fixed value that collides with none of the run
# config's pinned controller tokens nor the tool's "dcs-pltc".
REARM_FOREIGN = 0x7265_6172_6d  # "rearm"


def _rearm_owner(response):
    """The owner token a fencing verdict attributes the standing
    claim to — the `owner` field the plant's `fenced` and `io.fenced`
    answers both carry under the loss-attribution contract — or None
    on an unfenced answer or a build predating the field."""
    return ((response or {}).get('error') or {}).get('owner')


def _rearm_unsupported(response):
    """Whether a claim-verb answer is the wire's `invalid_request` —
    a rig whose release predates the lifecycle verbs."""
    error = (response or {}).get('error') or {}
    return error.get('kind') == 'invalid_request'


def _rearm_settled(ctx, deadline):
    """The endpoint key of the settled field owner restricted to the
    pair containers — the restartee and the armed peer this leg
    cycles."""
    return wait_for(
        lambda: next(
            (name for name in ('active', 'standby')
             if (_try_role(ctx, ctx.get(name) or '')
                 or {}).get('role') == 'active'),
            None) or None,
        deadline, interval=REARM_POLL)


def _rearm_switch(ctx, armed, note):
    """The documented switch onto the armed peer — demote the
    settled owner, promote the armed standby, and wait for the
    armed report to settle — returns the demoted owner's endpoint
    key, or None where the switch never landed."""
    owner = _rearm_settled(ctx, time.monotonic() + REARM_SETTLE)
    if owner is None:
        note('no-owner', 'no pair peer reports role=active — the '
             'switch has no field owner to move')
        return None
    if owner == armed:
        return armed
    status, body = _settle_call(ctx[owner] + '/demote')
    if status != 200:
        note('switch-demote-refused',
             'POST /demote on the settled owner answered '
             + str(status) + ' ' + json.dumps(body)[:200])
        return None
    deadline = time.monotonic() + REARM_SWITCH
    promoted = None
    while time.monotonic() < deadline and promoted is None:
        status, body = _settle_call(ctx[armed] + '/promote')
        if status == 200:
            promoted = body
        else:
            time.sleep(REARM_POLL)
    if promoted is None:
        note('switch-promote-refused',
             'POST /promote on the armed peer never landed — it '
             'never converged tracking for the switch')
        return None
    settled = wait_for(
        lambda: (_try_role(ctx, ctx[armed]) or {}).get('role')
                == 'active' or None,
        deadline, interval=REARM_POLL)
    if not settled:
        note('switch-never-settled',
             'the armed peer never reported active after its '
             'promotion')
        return None
    return owner


def _rearm_probe_judge(verdict, token, note):
    """Judge a foreign conditional claimer's answer against the
    re-bound incumbent: `fenced` naming the incumbent is the
    contract's verdict — a grant is the defect the finding records,
    the yield mark outliving the same-owner re-grant — and any
    other answer is outside the contract's vocabulary. Returns the
    digest word: 'named', 'misnamed', 'granted', or 'odd'."""
    if _fenced(verdict):
        named = _rearm_owner(verdict)
        if named is None:
            raise ConnectionError('the fencing verdict names no '
                                  'standing owner — the rig predates '
                                  'the loss-attribution contract: '
                                  + json.dumps(verdict)[:300])
        if named != token:
            note('probe-misnamed',
                 'the fencing verdict attributes the re-armed '
                 'claim to ' + str(named) + ', not the re-bound '
                 'incumbent\'s token: ' + json.dumps(verdict)[:300])
            return 'misnamed'
        return 'named'
    if (verdict or {}).get('result') in ('done', 'claimed_shared'):
        note('probe-granted',
             'a different owner\'s conditional claim preempted '
             'the re-bound live incumbent — the yield mark still '
             'stands: ' + json.dumps(verdict)[:300])
        return 'granted'
    note('probe-odd',
         'the foreign conditional probe answered outside the '
         'contract\'s vocabulary: ' + json.dumps(verdict)[:300])
    return 'odd'


def _rearm_row_judge(row, token, note):
    """Judge one restartee-window poll row: the re-bound incumbent
    must keep reporting active, the restartee must never reach the
    field-owning walk its refused startup claim exits ahead of, and
    a third-party mutation must keep fencing on the incumbent's
    token. Returns 'clean' or 'breached'."""
    verdict = 'clean'
    armed = row.get('armed')
    if armed is None:
        verdict = 'breached'
        note('successor-silent',
             'the live successor\'s monitor stopped answering '
             'through the restartee\'s claim')
    elif armed != 'active':
        verdict = 'breached'
        note('successor-left-active',
             'the re-bound incumbent left active through the '
             'restartee\'s claim — the yield mark outlived the '
             're-grant: ' + json.dumps(row)[:300])
    if row.get('restartee') in ('active', 'promoting'):
        verdict = 'breached'
        note('restartee-preempted',
             'the cold-restarted peer\'s startup claim preempted '
             'the re-bound incumbent and reached '
             + str(row['restartee']) + ' — the stale-resume '
             'seizure the conditional grant exists to refuse')
    probe = row.get('probe')
    if probe is not None:
        if probe != 'fenced':
            verdict = 'breached'
            note('claim-open',
                 'a third-party mutation landed through the '
                 'restartee\'s claim — the probe answered '
                 + str(probe))
        elif row.get('probe_owner') != token:
            verdict = 'breached'
            note('claim-moved',
                 'the standing claim moved off the re-bound '
                 'incumbent\'s token to '
                 + str(row.get('probe_owner'))
                 + ' — the restartee\'s conditional grant '
                 'preempted it')
    return verdict


def _rearm_incumbent_judge(entries, note):
    """Audit one journal slice — served or durable — from the
    re-bound incumbent's restartee window: a `field_claim_lost` or
    a demoting role walk is the signature of the preemption the
    contract refuses. Returns 'clean' or 'breached'."""
    verdict = 'clean'
    for entry in entries or []:
        event = (entry or {}).get('event') or {}
        if 'field_claim_lost' in event:
            verdict = 'breached'
            note('incumbent-loss',
                 'the re-bound incumbent journaled a fencing loss '
                 'through the restartee\'s claim — its live claim '
                 'was preempted: ' + json.dumps(entry)[:300])
        change = event.get('role_changed') or {}
        if change.get('to') in ('demoting', 'standby'):
            verdict = 'breached'
            note('incumbent-demoted',
                 'the re-bound incumbent journaled a demotion '
                 '(origin ' + str(change.get('origin')) + ') '
                 'through the restartee window: '
                 + json.dumps(entry)[:300])
    return verdict


def _rearm_restartee_judge(entries, note):
    """Audit the restartee's durable window: a role walk into the
    field-owning states means its startup claim landed where the
    re-bound incumbent's live claim must have fenced it. Returns
    'clean' or 'breached'."""
    verdict = 'clean'
    for entry in entries or []:
        change = (((entry or {}).get('event') or {})
                  .get('role_changed') or {})
        if change.get('to') in ('promoting', 'active'):
            verdict = 'breached'
            note('restartee-claimed',
                 'the restartee\'s durable journal walks it '
                 + str(change.get('from')) + ' -> '
                 + str(change.get('to')) + ' — its startup claim '
                 'landed on the field: ' + json.dumps(entry)[:300])
    return verdict


def _rearm_file_floor(path):
    """The newest entry seq a `--journal-file` holds — the durable
    cursor the window audit reads above — or None while the file
    cannot be read."""
    try:
        seqs = [(item.get('entry') or {}).get('seq')
                for item in _journal_entries(path)]
        seqs = [seq for seq in seqs if isinstance(seq, int)]
    except Exception:
        return None
    return max(seqs) if seqs else 0


def _rearm_file_entries(path, floor):
    """The durable `--journal-file` entries above `floor`, or None
    while the file cannot be read — the durable half of the window
    audit."""
    if path is None or floor is None:
        return None
    try:
        return [(item.get('entry') or {})
                for item in _journal_entries(path)
                if isinstance((item.get('entry') or {}).get('seq'),
                              int)
                and item['entry']['seq'] > floor]
    except Exception:
        return None


def _rearm_socket(ctx, armed, token, note, evidence):
    """The socket-level shape the finding records, re-staged on the
    re-bound incumbent's own claim: an attachment joins the standing
    claim under the incumbent's pinned token, yields it through
    `release_writer{keep_claim}`, re-grants it same-owner through
    `claim_writer_unless_held` — the re-arm that must clear the
    mark — and a different owner's conditional claimer must answer
    fenced naming the re-bound owner. Returns the digest word."""
    owner_io = _plant_connect(ctx)
    regrant_io = _plant_connect(ctx)
    probe_io = _plant_connect(ctx)
    try:
        join = _plant_request(owner_io,
                              {'op': 'ensure_writer', 'owner': token})
        evidence['join'] = join
        if _rearm_unsupported(join):
            raise ConnectionError('ensure_writer answered '
                                  'invalid_request — the rig '
                                  'predates the claim lifecycle '
                                  'verbs: ' + json.dumps(join)[:300])
        if join.get('result') not in ('done', 'claimed_shared'):
            note('socket-join-refused',
                 'the shared-claim join under the re-bound '
                 'owner\'s token was refused: '
                 + json.dumps(join)[:300])
            return 'refused'
        yielded = _plant_request(owner_io, {'op': 'release_writer',
                                            'keep_claim': True})
        evidence['yield'] = yielded
        if _rearm_unsupported(yielded):
            raise ConnectionError('release_writer{keep_claim} '
                                  'answered invalid_request — '
                                  'the rig predates the yield '
                                  'lifecycle: '
                                  + json.dumps(yielded)[:300])
        if yielded.get('result') != 'done':
            note('socket-yield-refused',
                 'the keep-claim release on the re-bound claim '
                 'was refused: ' + json.dumps(yielded)[:300])
            return 'refused'
        grant = _plant_request(regrant_io,
                               {'op': 'claim_writer_unless_held',
                                'owner': token})
        evidence['regrant'] = grant
        if _rearm_unsupported(grant):
            raise ConnectionError('claim_writer_unless_held '
                                  'answered invalid_request — '
                                  'the rig predates the '
                                  'conditional grant: '
                                  + json.dumps(grant)[:300])
        if grant.get('result') not in ('done', 'claimed_shared'):
            note('socket-regrant-refused',
                 'the same-owner conditional re-grant over the '
                 're-bound claim was refused: '
                 + json.dumps(grant)[:300])
            return 'refused'
        probe = _plant_request(probe_io,
                               {'op': 'claim_writer_unless_held',
                                'owner': REARM_FOREIGN})
        evidence['foreign'] = probe
        if _rearm_unsupported(probe):
            raise ConnectionError('claim_writer_unless_held '
                                  'answered invalid_request — '
                                  'the rig predates the '
                                  'conditional grant: '
                                  + json.dumps(probe)[:300])
        verdict = _rearm_probe_judge(probe, token, note)
        if verdict == 'granted':
            # The induction grant now stands: release it so the
            # preempted incumbent's loss-marked reclaim can
            # re-seat the field, and wait out the re-seat before
            # the pass's restore takes over.
            try:
                _plant_request(probe_io, {'op': 'release_writer'})
            except Exception:
                pass
            reseated = wait_for(
                lambda: (_try_role(ctx, ctx[armed])
                         or {}).get('role') == 'active' or None,
                time.monotonic() + REARM_RECLAIM,
                interval=REARM_POLL)
            evidence['reclaim_reseated'] = bool(reseated)
        return verdict
    finally:
        for stream in (owner_io, regrant_io, probe_io):
            try:
                stream.close()
            except Exception:
                pass


def _rearm_restore(ctx, owner, armed, deadline):
    """Best-effort launch-layout restore inside one bound: demote
    the armed peer where it still owns the field — the keep-claim
    yield whose hand-off the owner's conditional startup claim
    preempts — start the exited owner container back through its
    startup claim, promote a converged one, and wait for the
    pair's tracking posture."""
    while time.monotonic() < deadline:
        current = _pair_active(ctx)
        if current == owner \
                and _tracking_standby(ctx, armed) is not None:
            return True
        if current == armed:
            _settle_call(ctx[armed] + '/demote')
        elif current != owner:
            report = _try_role(ctx, ctx[owner])
            if report is None:
                try:
                    ctx['start_controller'](owner)
                except Exception:
                    pass
            elif report.get('role') == 'standby' \
                    and set(report.get('sync') or {}) \
                    & {'tracking', 'orphaned', 'reinitialized'}:
                _settle_call(ctx[owner] + '/promote')
        time.sleep(REARM_POLL)
    return _pair_active(ctx) == owner \
        and _tracking_standby(ctx, armed) is not None


def _rearm_pass(ctx, number, armed, restartee, tokens, budget,
                journal_files):
    """One re-arm pass over the deployed pair: settle the launch
    layout, switch the field onto the failover-armed peer, demote
    it so the yielded claim stands under its token, let its own
    orphan failover re-promote it over its own standing claim —
    the same-owner re-grant the contract re-arms on — cold-restart
    the ex-owner and audit through both peers' serving monitors and
    both durable journals that the restartee's conditional startup
    claim fenced rather than preempting, then re-stage the socket
    shape on the re-bound claim and restore the launch roles.
    Returns (digest, violations, evidence): the digest is the
    pass's normalized verdict record, identical across clean
    passes."""
    violations = {}
    evidence = {'pass': number, 'armed': armed,
                'restartee': restartee}
    digest = {'settle': 'unseen', 'switched': 'unseen',
              'yielded': 'unseen', 'repromoted': 'unseen',
              'reseated': 'unseen', 'restartee': 'unseen',
              'incumbent': 'unseen', 'journals': 'unseen',
              'socket': 'unseen', 'roles': 'unseen'}
    armed_base, restartee_base = ctx[armed], ctx[restartee]
    armed_token, restartee_token = tokens[armed], tokens[restartee]

    def note(key, detail):
        violations.setdefault(key, (DIAG_FAILED, detail))

    def finish(result):
        evidence['digest'] = result
        evidence['violations'] = {key: {'diagnostic': name,
                                        'detail': detail}
                                  for key, (name, detail)
                                  in violations.items()}
        return result, violations, evidence

    # The settled gate: the launch owner holds the field and the
    # armed sibling tracks it — the layout the pass's restore owes.
    settled = wait_for(
        lambda: _pair_active(ctx) == restartee
                and _tracking_standby(ctx, armed) is not None
                or None,
        time.monotonic() + REARM_SETTLE, interval=REARM_POLL)
    if not settled:
        note('settle', 'the pair never settled to the launch '
             'layout — ' + restartee + ' owns no field with '
             + armed + ' tracking behind it')
        return finish(digest)
    digest['settle'] = 'launch'

    # The switch onto the armed peer — the failover-armed standby
    # must hold the field for its own demotion to yield the claim
    # under its token.
    demoted = _rearm_switch(ctx, armed, note)
    evidence['switched_off'] = demoted
    if demoted is None:
        return finish(digest)
    digest['switched'] = 'armed'

    # The demote: POST /demote on the armed field owner yields the
    # standing claim under its own token — the deliberate hand-off
    # a successor may preempt — while third-party mutations stay
    # fenced naming it.
    status, body = _settle_call(armed_base + '/demote')
    evidence['demote'] = {'status': status, 'report': body}
    if status != 200 or (body or {}).get('role') != 'demoting':
        note('demote-refused',
             'POST /demote on the armed field owner answered '
             + str(status) + ' ' + json.dumps(body)[:300])
        return finish(digest)
    settled = wait_for(
        lambda: (_try_role(ctx, armed_base) or {}).get('role')
                == 'standby' or None,
        time.monotonic() + REARM_DEMOTE, interval=REARM_POLL)
    if not settled:
        note('never-settled-standby',
             'the demoted armed owner never settled standby')
        return finish(digest)
    probe = _try_plant(ctx, {'op': 'step', 'dt': 0})
    evidence['yielded_probe'] = probe
    if not _fenced(probe) or _rearm_owner(probe) != armed_token:
        note('yield-missing',
             'the demote did not leave the claim yielded under '
             'the armed owner\'s token — the probe answered '
             + json.dumps(probe)[:300])
        return finish(digest)
    digest['yielded'] = 'standing'

    # The orphan failover: the armed peer's own scans pull its
    # demoted tracker's ownerless checkpoints — each orphaned apply
    # a heartbeat miss — until the budget-th apply self-promotes it
    # over its own yielded claim: the same-owner re-grant whose
    # re-arm the contract is.
    bound = time.monotonic() + REARM_REPROMOTE + budget * 0.2
    watch = []
    repromoted = None
    while time.monotonic() < bound and repromoted is None:
        report = _try_role(ctx, armed_base)
        partner = _try_role(ctx, restartee_base)
        watch.append({'armed': (report or {}).get('role'),
                      'restartee': (partner or {}).get('role'),
                      'sync': (report or {}).get('sync')})
        if (report or {}).get('role') == 'active':
            repromoted = report
        else:
            time.sleep(REARM_POLL)
    evidence['repromote'] = {'watch': watch[-8:],
                             'report': repromoted}
    if repromoted is None:
        note('never-repromoted',
             'the armed orphan never re-promoted over its own '
             'yielded claim inside the failover budget — the rig '
             'predates the orphan-failover claim path: '
             + json.dumps(watch[-4:])[:300])
        return finish(digest)
    if any(row['restartee'] not in ('standby', 'demoting', None)
           for row in watch):
        note('restartee-moved',
             'the demoted peer left standby through the orphan '
             'window: ' + json.dumps(watch[-4:])[:300])
    digest['repromoted'] = 'active'
    probe = _try_plant(ctx, {'op': 'step', 'dt': 0})
    evidence['reseated'] = probe
    if not _fenced(probe) or _rearm_owner(probe) != armed_token:
        note('reseat-missing',
             'the orphan re-promotion did not re-seat the armed '
             'peer\'s claim — the probe answered '
             + json.dumps(probe)[:300])
        return finish(digest)
    digest['reseated'] = 'named'

    # The journal floors the window audit reads above — re-read
    # here so the audit diffs only the restartee window: the demote
    # -> standby -> promote -> active walk the re-promotion itself
    # journaled is legitimately above any earlier floor. The
    # durable floors ride each file's own seq domain.
    try:
        _, journal = http_json('GET', armed_base + '/journal')
        entries = _journal_list(journal)
        floor = (entries[-1].get('seq') or 0) if entries else 0
    except Exception:
        floor = 0
    file_floors = {name: _rearm_file_floor(journal_files.get(name))
                   for name in (armed, restartee)}
    evidence['floors'] = {'served': floor, 'files': file_floors}

    # The restartee: the demoted peer cold-restarts as its
    # configured active — its deployment shape — and its startup
    # conditional claim meets the re-granted claim. The claim must
    # read as the live incumbent's unyielded hold again: the
    # restartee's activation refuses and exits rather than
    # preempting the failover successor mid-run — the
    # stale-resume seizure the conditional grant exists to refuse.
    ctx['cold_restart_controller'](restartee)
    window = []
    time.sleep(REARM_GRACE)
    for index in range(REARM_ROUNDS):
        report = _try_role(ctx, armed_base)
        partner = _try_role(ctx, restartee_base)
        probe = _try_plant(ctx, {'op': 'step', 'dt': 0})
        row = {'armed': (report or {}).get('role'),
               'restartee': (partner or {}).get('role'),
               'probe': _probe_error(probe),
               'probe_owner': _rearm_owner(probe)}
        window.append(row)
        _rearm_row_judge(row, armed_token, note)
        if index + 1 < REARM_ROUNDS:
            time.sleep(REARM_POLL)
    evidence['restart_window'] = window
    digest['restartee'] = 'preempted' \
        if any(key in violations for key in
               ('restartee-preempted', 'claim-moved',
                'restartee-moved')) else 'fenced'
    digest['incumbent'] = 'lost' \
        if any(key in violations for key in
               ('successor-silent', 'successor-left-active',
                'claim-open')) else 'held'

    # The incumbent's served journal above the floor: no fencing
    # loss, no demotion — the restartee's claim never touched it.
    try:
        _, body = http_json('GET', armed_base + '/journal?since='
                            + str(floor))
        incumbent_served = _journal_list(body)
    except Exception as exc:
        incumbent_served = None
        note('incumbent-journal',
             'the re-bound incumbent\'s journal never served the '
             'post-episode audit: ' + str(exc)[:200])
    evidence['incumbent_journal'] = incumbent_served
    served = 'unread' if incumbent_served is None \
        else _rearm_incumbent_judge(
            incumbent_served,
            lambda key, detail: note(key + '-served', detail))

    # The durable halves: each peer's --journal-file window —
    # the incumbent's for the same no-loss/no-demotion audit, the
    # restartee's for the activating walk its refused startup
    # claim never journals.
    durable = {name: _rearm_file_entries(journal_files.get(name),
                                         file_floors[name])
               for name in (armed, restartee)}
    evidence['durable'] = durable
    if durable.get(armed) is None:
        note('incumbent-durable',
             'the re-bound incumbent\'s durable journal never '
             'read for the window audit')
        d_armed = 'unread'
    else:
        d_armed = _rearm_incumbent_judge(
            durable[armed],
            lambda key, detail: note(key + '-durable', detail))
    if durable.get(restartee) is None:
        note('restartee-durable',
             'the restartee\'s durable journal never read for '
             'the window audit')
        d_rest = 'unread'
    else:
        d_rest = _rearm_restartee_judge(
            durable[restartee],
            lambda key, detail: note(key + '-durable', detail))
    digest['journals'] = 'clean' \
        if served == d_armed == d_rest == 'clean' else 'breached'

    # The socket-level shape the finding records, re-staged on the
    # re-bound claim itself: yield it again, re-grant it same-owner
    # through the conditional verb, and a different owner's
    # conditional claimer must answer fenced naming the incumbent.
    digest['socket'] = _rearm_socket(ctx, armed, armed_token,
                                     note, evidence)

    # The launch roles: the restartee container comes back through
    # its conditional startup claim — the yielded hand-off the
    # armed peer's own restore demote leaves — and the pair
    # reconverges to the launch layout.
    restored = _rearm_restore(ctx, restartee, armed,
                              time.monotonic() + REARM_RESTORE)
    digest['roles'] = 'restored' if restored else 'unrestored'
    if not restored:
        note('roles',
             'the pair did not land back on the launch roles — '
             'owner ' + str(_pair_active(ctx)))
    return finish(digest)


def _rearm_self_check():
    """The leg's unchecked-diagnostic self-test: run every judge
    over each planted negative it must name — a fencing loss and a
    fenced-origin demotion on the incumbent, an activating walk on
    the restartee, a granted or misnamed foreign conditional probe,
    and a breached restartee-window row — and require the judge to
    note its named violation. A silent judge returns the negative
    names it let through."""
    slipped = []

    def judge(fn, *args):
        found = []
        fn(*args, lambda key, detail: found.append(key))
        return found

    token = 0xC1A1
    # The incumbent audit: a planted field_claim_lost and a
    # planted origin=fenced demotion must each name the breach.
    loss = [{'seq': 9, 'tick': 9, 'event': {
        'field_claim_lost': {'point': 100, 'claimant': 7}}}]
    if not judge(_rearm_incumbent_judge, loss):
        slipped.append('incumbent-loss')
    demoted = [{'seq': 9, 'tick': 9, 'event': {'role_changed': {
        'from': 'active', 'to': 'demoting', 'origin': 'fenced'}}}]
    if not judge(_rearm_incumbent_judge, demoted):
        slipped.append('incumbent-demoted')
    # The restartee audit: a planted activating walk must name the
    # seizure.
    claimed = [{'seq': 4, 'tick': 4, 'event': {'role_changed': {
        'from': 'standby', 'to': 'active'}}}]
    if not judge(_rearm_restartee_judge, claimed):
        slipped.append('restartee-claimed')
    # The probe judge: a planted grant and a planted misnamed
    # fencing verdict must each name the breach.
    if _rearm_probe_judge({'result': 'done'}, token,
                          lambda key, detail: None) != 'granted':
        slipped.append('probe-granted')
    misnamed = {'result': 'error',
                'error': {'kind': 'fenced', 'owner': token + 1}}
    if _rearm_probe_judge(misnamed, token,
                          lambda key, detail: None) != 'misnamed':
        slipped.append('probe-misnamed')
    # The window judge: a planted preempted row and a planted
    # claim-moved row must each name the breach.
    row = {'armed': 'standby', 'restartee': 'active',
           'probe': 'fenced', 'probe_owner': token + 1}
    if _rearm_row_judge(row, token,
                        lambda key, detail: None) != 'breached':
        slipped.append('restartee-window')
    # The clean shapes must stay silent — a judge that names the
    # honest window is a judge that over-reports.
    clean_row = {'armed': 'active', 'restartee': None,
                 'probe': 'fenced', 'probe_owner': token}
    if _rearm_row_judge(clean_row, token,
                        lambda key, detail: None) != 'clean':
        slipped.append('clean-window')
    if _rearm_probe_judge({'result': 'error', 'error': {
            'kind': 'fenced', 'owner': token}}, token,
            lambda key, detail: None) != 'named':
        slipped.append('clean-probe')
    return slipped


def scenario_yielded_claim_rearm(ctx):
    """Exercise the #1123 yielded-claim re-arm contract on the
    deployed pair: with the pair settled and tracking, switch the
    field onto the failover-armed peer and demote it so the yielded
    claim stands under its token, let its own orphan failover
    re-promote it — the same-owner re-grant that re-binds it as the
    live owner of its own standing claim — cold-restart the
    ex-owner and assert through both peers' serving monitors and
    durable journals that the restartee's conditional startup
    claim fenced rather than preempting the re-bound incumbent,
    then re-stage the socket shape on the re-bound claim — a
    same-owner claim_writer_unless_held re-grant after a keep-claim
    yield, a foreign conditional claimer answering fenced — and
    restore the launch roles; two passes, identical digests."""
    case = Case('yielded-claim-rearm',
                'A yield-marked claim re-armed by its owner\'s '
                're-incumbency fences the restartee\'s '
                'conditional claim',
                'with the deployed pair settled and tracking, '
                'the documented switch puts the failover-armed '
                'peer on the field and POST /demote yields its '
                'claim under its pinned token; its own tracking '
                'scans pull the demoted tracker\'s ownerless '
                'checkpoints until the miss budget self-promotes '
                'it over its own yielded claim — the same-owner '
                're-grant that clears the mark — and the demoted '
                'ex-owner cold-restarts as its configured '
                'active: through both peers\' serving monitors '
                'and both durable journals the restartee\'s '
                'conditional startup claim answers fenced while '
                'the re-bound incumbent keeps the field and '
                'journals no field_claim_lost/origin=fenced '
                'demotion; the socket shape the finding records '
                're-stages on the re-bound claim — keep-claim '
                'yield, same-owner conditional re-grant, a '
                'foreign conditional claimer fenced naming the '
                'incumbent — and the pair\'s launch roles '
                'restore; two passes produce identical digests')
    owner, armed = 'active', 'standby'
    try:
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries only one endpoint — the '
                               'pair the re-arm leg needs is '
                               'absent')
        if not ctx.get('plant'):
            return case.finish('inconclusive', 'the run context '
                               'carries no plant endpoint — the '
                               'claim-aware attachment the leg\'s '
                               'fencing probes ride is absent')
        if ctx.get('cold_restart_controller') is None \
                or ctx.get('start_controller') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no controller lifecycle '
                               'actions — the restartee half '
                               'cannot run')
        budget = ctx.get('failover_misses')
        if not budget:
            return case.finish('inconclusive', 'the rig arms no '
                               'failover budget — the orphan '
                               're-promotion the contract rides '
                               'never runs')
        tokens = ctx.get('plant_owner') or {}
        if tokens.get(owner) is None or tokens.get(armed) is None:
            return case.finish('inconclusive', 'the run pins no '
                               'plant-writer owner tokens for the '
                               'pair endpoints')
        journal_files = ctx.get('journal_files') or {}
        missing = [name for name in (owner, armed)
                   if journal_files.get(name) is None]
        if missing:
            return case.finish('inconclusive', 'the run context '
                               'carries no journal files for '
                               + json.dumps(missing) + ' — the '
                               'durable half of the window audit '
                               'is absent')
        for name in (owner, armed):
            try:
                _role(ctx, ctx[name])
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s '
                                   'monitor is unreachable: '
                                   + str(exc)[:200])
        deadline = time.monotonic() + REARM_SETTLE
        active = wait_for(lambda: _pair_active(ctx), deadline,
                          interval=REARM_POLL)
        if active is None:
            return case.finish('failed', 'no peer reports '
                               'role=active')
        if active != owner:
            return case.finish('inconclusive', 'the field owner '
                               'is ' + active + ' — the re-arm '
                               'exercise needs the launched-'
                               'active peer owning the field; '
                               'the pair\'s layout predates the '
                               'stage')
        if wait_for(lambda: _tracking_standby(ctx, armed),
                    deadline, interval=REARM_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the armed '
                               'orphan the leg re-promotes was '
                               'never staged')
        # The contract surface: a third-party mutation must
        # already fence on the standing owner's token — an open
        # or unattributed field predates the claim lifecycle the
        # leg's probes read.
        probe = _try_plant(ctx, {'op': 'step', 'dt': 0})
        if not _fenced(probe):
            return case.finish('inconclusive', 'a third-party '
                               'mutation answered unfenced — '
                               'the rig predates the field '
                               'claim the leg exercises: '
                               + json.dumps(probe)[:300])
        if _rearm_owner(probe) is None:
            return case.finish('inconclusive', 'the fencing '
                               'verdict names no standing owner '
                               '— the rig predates the '
                               'loss-attribution contract')
        # The lifecycle verbs: the leg's socket stage rides
        # ensure/release_writer and the conditional grant — a rig
        # answering invalid_request predates the contract. The
        # probe attachment joins the standing owner's claim and
        # releases outright: a bare release only ever drops this
        # attachment's own hold.
        probe_io = _plant_connect(ctx)
        try:
            join = _plant_request(
                probe_io, {'op': 'ensure_writer',
                           'owner': tokens[owner]})
            if _rearm_unsupported(join):
                return case.finish(
                    'inconclusive', 'ensure_writer answered '
                    'invalid_request — the rig predates the '
                    'claim lifecycle verbs: '
                    + json.dumps(join)[:300])
            if join.get('result') not in ('done', 'claimed_shared'):
                return case.finish(
                    'inconclusive', 'the claim-aware attachment '
                    'cannot join the standing claim under the '
                    'owner\'s pinned token: '
                    + json.dumps(join)[:300])
            release = _plant_request(probe_io,
                                     {'op': 'release_writer'})
            if _rearm_unsupported(release):
                return case.finish(
                    'inconclusive', 'release_writer answered '
                    'invalid_request — the rig predates the '
                    'claim lifecycle verbs: '
                    + json.dumps(release)[:300])
        finally:
            try:
                probe_io.close()
            except Exception:
                pass
        case.observe('field owner: ' + owner + ' (' + ctx[owner]
                     + ') under pinned token '
                     + hex(tokens[owner]) + '; armed sibling: '
                     + armed + ' under ' + hex(tokens[armed]))
        digests = []
        try:
            for number in (1, 2):
                digest, violations, evidence = _rearm_pass(
                    ctx, number, armed, owner, tokens, budget,
                    journal_files)
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'yielded-claim-rearm-pass-'
                    + str(number) + '.json', evidence)
                case.evidence('file', ref, 'yielded-claim re-arm '
                              'pass ' + str(number) + ' — the '
                              'switch, the yield, the orphan '
                              're-promotion, the restartee '
                              'window, the journal audits, and '
                              'the socket re-stage')
                if evidence.get('inconclusive'):
                    return case.finish(
                        'inconclusive',
                        evidence['inconclusive'])
                if violations or digest is None:
                    return case.finish(
                        'failed', DIAG_FAILED + ': ' + '; '.join(
                            detail for _, detail in
                            list(violations.values())[:4]))
                digests.append(digest)
        finally:
            # The launch layout for the legs behind this one: a
            # clean pass restores it by construction; an aborted
            # pass may have left the armed peer owning the field
            # and the ex-owner exited — demote the armed peer so
            # its claim yields, start the launch owner back
            # through its conditional startup claim, and let the
            # pair reconverge.
            try:
                if not _rearm_restore(
                        ctx, owner, armed,
                        time.monotonic() + REARM_RESTORE):
                    case.observe('cleanup: the pair did not '
                                 'settle back to the launch '
                                 'roles')
            except Exception as exc:
                case.observe('cleanup: role restore failed: '
                             + str(exc)[:200])
        if digests[0] != digests[1]:
            return case.finish(
                'failed', DIAG_NONDET + ': the two passes\' '
                'digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two re-arm passes, identical digests')

        # The unchecked self-check: each judge, run over a
        # planted negative, must name its violation — a silent
        # audit can no longer be trusted to catch what it names.
        slipped = _rearm_self_check()
        if slipped:
            return case.finish(
                'failed', DIAG_UNCHECKED + ': the re-arm audits '
                'stayed silent on planted negatives: '
                + ', '.join(slipped))
        case.observe('the self-check leg\'s planted negatives '
                     'each reported their named violation')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
