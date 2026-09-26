"""The claim_reclaim acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: The claim-reclaim case restores what it moves: the
# preempted owner re-seats the field through its own loss-marked
# reclaim and the pair lands back on the launch claim state and
# roles, so it needs no declared window.


# --------------------------------------------------------------------
# The released-preemption fencing-loss reclaim contract (WW-LCM-001's
# continuity clause, WW-OPS-003's field-confidence clause — the
# per-revision lane evidence for decision 97's contract, landed via
# #935): a foreign attachment's `claim_writer` preempts the live
# field owner unconditionally; the owner's first fenced write
# demotes it in place with the fencing loss journaled naming the
# winning claimant; and once the preemptor releases, the demoted
# ex-owner's fencing-loss mark drives a *bound* conditional
# `ensure_writer` every standby scan — the re-grant that joins the
# run's attachments to the claim's holders and refuses, never
# preempts, a different-owner claim — so the released-preemption
# wedge recovers unattended, without an operator restart. The
# unclaimed-rearm leg exercises only the fast preempt-and-release
# window where the owner never fences; this leg holds the foreign
# claim through the fenced-write demotion and proves the slower
# path: the loss journal's winning-claimant attribution, the bound
# grant's refusal while a different-owner claim stands, and the
# release-triggered re-seat that lands the ex-owner's writes and
# reconverges the pair. The claim ops stay on the raw client —
# `claim_writer` is an op `dcs-plant-ctl` does not expose — while
# the census and field reads ride the shipped tool. The named
# diagnostics are claim-reclaim-failed and
# claim-reclaim-nondeterministic; two consecutive passes must
# produce identical digests.

CLAIM_RECLAIM_SETTLE = 30    # bound on the pair reporting settled
CLAIM_RECLAIM_POLL = 0.4     # cadence polling the peers mid-episode
CLAIM_RECLAIM_ROUNDS = 6     # polls the held-claim window must span
CLAIM_RECLAIM_DEADLINE = 15  # bound on the demotion/reclaim waits
CLAIM_RECLAIM_RESTORE = 10   # grace the finally gives a pending reclaim
# The dedicated attachment's foreign owner token — never a
# controller's pinned token nor the tool's "dcs-pltc": the
# preempting claim whose hold fences the owner and whose release
# triggers the loss-marked reclaim.
CLAIM_RECLAIM_FOREIGN = 0x7161_2d63_6c61_696d  # "qa-claim"


def _write_fenced(response):
    """Whether a write probe's answer is the point-level fencing
    verdict — `{"kind":"io","error":{"fenced":N}}` — the refusal the
    standing claim gives a non-holder's write."""
    error = (response or {}).get('error') or {}
    inner = error.get('error')
    return error.get('kind') == 'io' and isinstance(inner, dict) \
        and 'fenced' in inner


def _verdict_owner(response):
    """The owner token a fencing verdict attributes the standing
    claim to — the `owner` field the plant's `fenced` and `io.fenced`
    answers both carry under decision 97's attribution — or None on
    an unfenced answer or a build predating the field."""
    return ((response or {}).get('error') or {}).get('owner')


def _claim_reclaim_pass(ctx, active, peer, owner, watch, probe_point):
    """One induction pass: the dedicated attachment's claim_writer
    preempts the field owner and holds the claim through the
    fenced-write demotion, the held window proves the marked
    ex-owner's bound re-grant refuses a different-owner claim, and
    the release lets that bound grant re-seat the claim and walk
    the peer back to active. Returns (digest, violations,
    evidence): digest is the pass's normalized verdict record,
    identical across clean passes; violations is {key:
    (diagnostic, detail)} in first-seen order."""
    base, peer_base = ctx[active], ctx[peer]
    violations = {}
    evidence = {'watch_point': watch, 'probe_point': probe_point,
                'owner': owner, 'foreign': CLAIM_RECLAIM_FOREIGN}

    def note(key, diagnostic, detail):
        violations.setdefault(key, (diagnostic, detail))

    def failed(key, detail):
        note(key, 'claim-reclaim-failed', detail)

    # The audit positions the episode diffs against: each peer's
    # journal floor, the owner's fencing-loss ledger, and the
    # watched field output's stamp.
    floors = {}
    for name in (active, peer):
        _, journal = http_json('GET', ctx[name] + '/journal')
        entries = _journal_list(journal)
        floors[name] = (entries[-1].get('seq') or 0) if entries else 0
    snap0 = _try_snapshot(ctx, base) or {}
    ledger0 = (snap0.get('io_health') or {}).get('failed_writes') or 0
    sample0 = _probe_sample(ctx, watch)
    probe0 = _try_plant(ctx, {'op': 'step', 'dt': 0})
    evidence['baseline'] = {'floors': floors, 'field': sample0,
                            'failed_writes': ledger0, 'probe': probe0,
                            'tick': snap0.get('tick')}
    if probe0 is None:
        raise ConnectionError('the simulated plant never answered '
                              'the baseline fencing probe')
    if not _fenced(probe0):
        raise ConnectionError('the field held no standing writer '
                              'claim at pass start — the induction\'s '
                              'standing claim was never there: '
                              + json.dumps(probe0)[:300])
    if _verdict_owner(probe0) is None:
        raise ConnectionError('the fencing verdict names no standing '
                              'owner — the rig predates the '
                              'loss-attribution contract: '
                              + json.dumps(probe0)[:300])
    if _verdict_owner(probe0) != owner:
        raise ConnectionError('the standing claim names a foreign '
                              'token, not the launch owner — the rig '
                              'is not in its launch claim state: '
                              + json.dumps(probe0)[:300])

    # The induction: claim_writer preempts unconditionally and the
    # attachment holds it — the slow window the unclaimed-rearm
    # leg's same-breath release never opens.
    stream = _plant_connect(ctx)
    verdict = seized = None
    demoted = released = reclaimed = False
    hold_clean = landed = reconverged = False
    loss_state = 'silent'
    last_field = 0
    try:
        claim = _plant_request(stream, {'op': 'claim_writer',
                                        'owner':
                                        CLAIM_RECLAIM_FOREIGN})
        evidence['claim'] = claim
        verdict = claim.get('result')
        if verdict == 'claimed_shared':
            note('claim-shared', 'claim-reclaim-nondeterministic',
                 'the preempting claim joined a live foreign holder '
                 '— a leaked attachment shares the induction token: '
                 + json.dumps(claim)[:200])
        elif verdict != 'done':
            raise ConnectionError('the preempting claim was refused: '
                                  + json.dumps(claim)[:300])

        # The preemption registered under the induction token: a
        # third-party probe meets the fence attributing the standing
        # claim to it.
        seized = _try_plant(ctx, {'op': 'step', 'dt': 0})
        evidence['seized'] = seized
        if seized is None:
            raise ConnectionError('the plant never answered the '
                                  'post-preemption fencing probe')
        if not _fenced(seized):
            failed('preemption-open', 'the field accepted a '
                   'third-party mutation under the preempted '
                   'claim: ' + json.dumps(seized)[:300])
        elif _verdict_owner(seized) != CLAIM_RECLAIM_FOREIGN:
            failed('preemption-misnamed', 'the fencing verdict '
                   'attributes the preempted claim to '
                   + str(_verdict_owner(seized))
                   + ', not the induction token: '
                   + json.dumps(seized)[:300])

        # The field freezes at the preemption: the owner's writes
        # fence from here and the induction attachment never
        # writes, so any advance is a foreign write landing.
        anchor = wait_for(lambda: _probe_sample(ctx, watch),
                          time.monotonic() + CLAIM_RECLAIM_DEADLINE,
                          interval=CLAIM_RECLAIM_POLL)
        if not isinstance(anchor, dict) \
                or not isinstance(anchor.get('tick'), int):
            raise ConnectionError('the field read anchoring the '
                                  'frozen-field check never answered '
                                  'a stamped sample')
        anchor = last_field = anchor['tick']

        # The demotion watch: the owner's first fenced write demotes
        # it in place — its monitor answering every poll, because a
        # degrade is not a death — while the peer holds standby.
        demotion = {'polls': 0, 'answered': 0, 'owner': [], 'peer': []}
        deadline = time.monotonic() + CLAIM_RECLAIM_DEADLINE
        while time.monotonic() < deadline and not demoted:
            demotion['polls'] += 1
            report = _try_role(ctx, base)
            partner = _try_role(ctx, peer_base)
            if report is not None:
                demotion['answered'] += 1
                demotion['owner'].append(report.get('role'))
                demoted = report.get('role') == 'standby'
            if partner is not None:
                demotion['peer'].append(partner.get('role'))
            if not demoted:
                time.sleep(CLAIM_RECLAIM_POLL)
        evidence['demotion'] = demotion
        if demotion['answered'] != demotion['polls']:
            failed('owner-killed', 'the fenced write took the '
                   'owner\'s monitor down — a kill, not the '
                   'demote-in-place the contract settles')
        if not demoted:
            failed('never-demoted', 'the owner\'s fenced write '
                   'never demoted it in place — the reported role '
                   'stayed ' + json.dumps(demotion['owner'][-3:]))
        if any(role not in ('active', 'demoting', 'standby')
               for role in demotion['owner']):
            failed('owner-off-contract', 'the demoting owner '
                   'reported an off-contract role: '
                   + json.dumps(demotion['owner']))
        if any(role != 'standby' for role in demotion['peer']):
            failed('peer-moved', 'the tracking peer reported a '
                   'role change through the preemption: '
                   + json.dumps(demotion['peer'][-3:]))

        # The journaled loss: exactly one field_claim_lost above
        # the floor — one per held claim — attributed to the
        # induction token the field's own fencing verdict named.
        entries = []

        def journaled():
            try:
                _, body = http_json('GET', base + '/journal?since='
                                    + str(floors[active]))
            except Exception:
                return None
            found = _journal_list(body)
            if any('field_claim_lost' in (entry.get('event') or {})
                   for entry in found):
                return found
            return None
        entries = wait_for(journaled,
                           time.monotonic() + CLAIM_RECLAIM_DEADLINE,
                           interval=CLAIM_RECLAIM_POLL) or []
        losses = [(entry.get('event') or {})['field_claim_lost']
                  for entry in entries
                  if 'field_claim_lost' in (entry.get('event') or {})]
        evidence['loss'] = {'entries': entries, 'losses': losses}
        if not losses:
            failed('loss-silent', 'the demotion left no '
                   'field_claim_lost on the owner\'s journal — '
                   'the preemption went unrecorded')
        elif len(losses) != 1:
            loss_state = 'duplicated'
            failed('loss-duplicated', 'expected exactly one '
                   'field_claim_lost above the journal floor, '
                   'found ' + str(len(losses)))
        else:
            loss = losses[0]
            if 'claimant' not in loss:
                loss_state = 'unattributed'
                failed('loss-unattributed', 'the journaled '
                       'fencing loss names no claimant — the '
                       'audit cannot attribute the takeover: '
                       + json.dumps(loss)[:200])
            elif loss.get('claimant') != CLAIM_RECLAIM_FOREIGN:
                loss_state = 'misattributed'
                failed('loss-misattributed', 'the journaled '
                       'fencing loss attributes the takeover to '
                       + str(loss.get('claimant'))
                       + ', not the induction token')
            else:
                loss_state = 'attributed'

        # The held window: the induction attachment holds the
        # foreign claim while the marked ex-owner's bound
        # conditional re-grant probes every standby scan — each
        # must refuse: the peer stays demoted, the verdict keeps
        # naming the induction token, the frozen field records no
        # foreign write, and third-party mutations stay fenced.
        probe_sample0 = _probe_sample(ctx, probe_point)
        if not isinstance(probe_sample0, dict):
            raise ConnectionError('the field read sizing the '
                                  'foreign write probe never '
                                  'answered')
        value = probe_sample0.get('value') or {'bool': True}
        foreign_write = _try_plant(ctx, {'op': 'write',
                                         'point': probe_point,
                                         'value': value})
        evidence['foreign_write'] = foreign_write
        hold_clean = True
        if foreign_write is not None:
            if not _write_fenced(foreign_write):
                hold_clean = False
                failed('foreign-write-landed', 'a third-party '
                       'write landed under the held foreign '
                       'claim: ' + json.dumps(foreign_write)[:300])
            elif _verdict_owner(foreign_write) \
                    != CLAIM_RECLAIM_FOREIGN:
                hold_clean = False
                failed('foreign-write-misnamed', 'the write '
                       'verdict attributes the standing claim '
                       'to ' + str(_verdict_owner(foreign_write))
                       + ', not the induction token: '
                       + json.dumps(foreign_write)[:300])
        hold = []
        for index in range(CLAIM_RECLAIM_ROUNDS):
            report = _try_role(ctx, base)
            partner = _try_role(ctx, peer_base)
            sample = _probe_sample(ctx, watch)
            probe = _try_plant(ctx, {'op': 'step', 'dt': 0})
            row = {'owner': (report or {}).get('role'),
                   'peer': (partner or {}).get('role'),
                   'field_tick': (sample or {}).get('tick'),
                   'probe': _probe_error(probe),
                   'probe_owner': _verdict_owner(probe)}
            hold.append(row)
            if report is None:
                hold_clean = False
                failed('owner-silent', 'the demoted owner\'s '
                       'monitor stopped answering through the '
                       'held window')
            elif report.get('role') != 'standby':
                hold_clean = False
                failed('owner-left-standby', 'the marked '
                       'ex-owner left standby while a '
                       'different-owner claim stood — the bound '
                       'grant took what it must refuse: '
                       + json.dumps(report)[:300])
            if partner is not None \
                    and partner.get('role') != 'standby':
                hold_clean = False
                failed('peer-moved-hold', 'the tracking peer '
                       'left standby through the held window: '
                       + json.dumps(partner)[:300])
            if probe is not None:
                if not _fenced(probe):
                    hold_clean = False
                    failed('hold-open', 'the held foreign claim '
                           'let a third-party mutation through: '
                           + json.dumps(probe)[:300])
                elif _verdict_owner(probe) \
                        != CLAIM_RECLAIM_FOREIGN:
                    hold_clean = False
                    failed('hold-preempted', 'the standing '
                           'claim moved off the induction '
                           'token — the bound grant preempted '
                           'the different-owner claim it must '
                           'refuse: ' + json.dumps(probe)[:300])
            ftick = row['field_tick']
            if isinstance(ftick, int):
                if ftick != anchor:
                    hold_clean = False
                    failed('foreign-write', 'the field moved '
                           'under the held foreign claim — a '
                           'foreign write landed: tick '
                           + str(anchor) + ' -> ' + str(ftick))
                last_field = ftick
            if index + 1 < CLAIM_RECLAIM_ROUNDS:
                time.sleep(CLAIM_RECLAIM_POLL)
        evidence['hold'] = hold

        # The release: the preemptor hands the claim back — the
        # ownerless window behind the demoted ex-owner's live
        # connection that its fencing-loss mark reclaims from.
        release = _plant_request(stream, {'op': 'release_writer'})
        evidence['release'] = release
        released = release.get('result') == 'done'
        if not released:
            raise ConnectionError('the preemptor\'s claim '
                                  'hand-back was refused: '
                                  + json.dumps(release)[:300])

        # The reclaim watch: the marked ex-owner's bound
        # conditional re-grant lands the first scan the field
        # stands unclaimed — re-seating the claim under its own
        # token and walking standby -> promoting -> active with
        # no operator call and no restart.
        reclaim = {'polls': 0, 'answered': 0,
                   'owner': [], 'peer': []}
        deadline = time.monotonic() + CLAIM_RECLAIM_DEADLINE
        while time.monotonic() < deadline and not reclaimed:
            reclaim['polls'] += 1
            report = _try_role(ctx, base)
            partner = _try_role(ctx, peer_base)
            if report is not None:
                reclaim['answered'] += 1
                reclaim['owner'].append(report.get('role'))
                reclaimed = report.get('role') == 'active'
            if partner is not None:
                reclaim['peer'].append(partner.get('role'))
            if not reclaimed:
                time.sleep(CLAIM_RECLAIM_POLL)
        evidence['reclaim'] = reclaim
        if reclaim['answered'] != reclaim['polls']:
            failed('owner-restart', 'the ex-owner\'s monitor '
                   'stopped answering across the reclaim — a '
                   'restart recovered what the bound grant '
                   'should have')
        if not reclaimed:
            failed('never-reclaimed', 'the released field was '
                   'never re-seated — the loss-marked reclaim '
                   'never ran or refused the unclaimed field: '
                   'reported roles '
                   + json.dumps(reclaim['owner'][-4:]))
        if any(role not in ('demoting', 'standby', 'promoting',
                            'active')
               for role in reclaim['owner']):
            failed('reclaim-off-contract', 'the reclaiming '
                   'owner reported an off-contract role: '
                   + json.dumps(reclaim['owner']))
        if any(role != 'standby' for role in reclaim['peer']):
            failed('peer-moved-reclaim', 'the tracking peer '
                   'left standby through the reclaim: '
                   + json.dumps(reclaim['peer'][-3:]))

        # The re-seat is bound: the re-lifted gate's writes land
        # again — a holderless re-take would fence the owner's
        # own first write.
        def advancing():
            _try_snapshot(ctx, base)  # the owner's scans keep running
            sample_ = _probe_sample(ctx, watch)
            if not isinstance(sample_, dict):
                return None
            return sample_ if isinstance(sample_.get('tick'), int) \
                and sample_['tick'] > last_field else None
        landed = wait_for(advancing,
                          time.monotonic() + CLAIM_RECLAIM_DEADLINE,
                          interval=CLAIM_RECLAIM_POLL) is not None
        if not landed:
            failed('writes-stalled', 'the re-seated claim fences '
                   'the owner\'s own writes — the re-grant never '
                   'joined the holders (field tick held at '
                   + str(last_field) + ')')

        # The re-seated claim stands under the launch owner's
        # token: a third-party probe meets the fence naming it.
        reseated = _try_plant(ctx, {'op': 'step', 'dt': 0})
        evidence['reseated'] = reseated
        if reseated is None:
            raise ConnectionError('the plant never answered the '
                                  'post-reclaim fencing probe')
        if not _fenced(reseated):
            failed('reseated-open', 'the re-seated claim does '
                   'not fence third-party mutations: '
                   + json.dumps(reseated)[:300])
        elif _verdict_owner(reseated) != owner:
            failed('reseated-foreign', 'the re-seated claim '
                   'names ' + str(_verdict_owner(reseated))
                   + ', not the launch owner — the re-grant '
                   'took under a foreign token: '
                   + json.dumps(reseated)[:300])

        # The reconvergence: exactly one active — the launch
        # field owner re-seated — plus one tracking standby, no
        # operator call having run.
        reconverged = wait_for(
            lambda: (_settled_active(ctx) == active or None)
            and _tracking_standby(ctx, peer),
            time.monotonic() + CLAIM_RECLAIM_SETTLE,
            interval=CLAIM_RECLAIM_POLL) is not None
        evidence['reconverged'] = reconverged
        if not reconverged:
            failed('never-reconverged', 'the pair never '
                   'reconverged to ' + active + ' active plus '
                   + peer + ' tracking standby after the '
                   'reclaim')

        # The journal audit above the floor: the walk back to
        # active journals as ordinary role changes — the
        # unattended recovery's record — while the tracking
        # peer's journal carries no loss and no role change.
        try:
            _, body = http_json('GET', base + '/journal?since='
                                + str(floors[active]))
            owner_journal = _journal_list(body)
        except Exception as exc:
            owner_journal = None
            failed('journal-owner', 'the owner\'s journal never '
                   'served the post-episode audit: '
                   + str(exc)[:200])
        try:
            _, body = http_json('GET', peer_base
                                + '/journal?since='
                                + str(floors[peer]))
            peer_journal = _journal_list(body)
        except Exception:
            peer_journal = None
        evidence['journal'] = {'owner': owner_journal,
                               'peer': peer_journal}
        if owner_journal is not None:
            walk = [(change.get('from'), change.get('to'))
                    for entry in owner_journal
                    for change in
                    [(entry.get('event') or {})
                     .get('role_changed') or {}]
                    if change]
            if reclaimed and ('promoting', 'active') not in walk \
                    and ('standby', 'active') not in walk:
                failed('reclaim-unrecorded', 'the unattended '
                       'reclaim left no journaled walk back to '
                       'active — an operator or restart path '
                       'ran instead: ' + json.dumps(walk))
        if peer_journal is not None:
            for entry in peer_journal:
                event = entry.get('event') or {}
                if 'field_claim_lost' in event:
                    failed('peer-claim-lost', 'the tracking '
                           'peer journaled a fencing loss it '
                           'never owned')
                if 'role_changed' in event:
                    failed('peer-role-journaled', 'the tracking '
                           'peer journaled a role change '
                           'through the episode')
    finally:
        stream.close()
        if not released:
            # Best effort: a stranded foreign hold — or a
            # holderless foreign claim standing on the
            # never-released rule — keeps fencing the ex-owner's
            # bound re-grant. Re-claim and release the induction
            # token through a fresh attachment so the field
            # stands unclaimed for the reclaim.
            try:
                restore = _plant_connect(ctx)
                try:
                    _plant_request(restore, {'op': 'claim_writer',
                                             'owner':
                                             CLAIM_RECLAIM_FOREIGN})
                    _plant_request(restore,
                                   {'op': 'release_writer'})
                finally:
                    restore.close()
            except Exception:
                pass  # an unreachable plant is the pass's own verdict
        # Best effort: the launch role layout for the legs
        # behind this one. Give a pending bound re-grant its
        # grace — the freed field may already be re-seating —
        # then the documented operator promote is the fallback
        # on a wedge; a peer moved off standby demotes back. A
        # clean pass moved nothing, so neither restore fires.
        wait_for(lambda: (_try_role(ctx, base) or {}).get('role')
                 == 'active' or None,
                 time.monotonic() + CLAIM_RECLAIM_RESTORE,
                 interval=CLAIM_RECLAIM_POLL)
        if (_try_role(ctx, base) or {}).get('role') != 'active':
            try:
                http_json('POST', base + '/promote')
            except Exception:
                pass
        if (_try_role(ctx, peer_base) or {}).get('role') \
                != 'standby':
            try:
                http_json('POST', peer_base + '/demote')
            except Exception:
                pass

    evidence['digest'] = {
        'claim': 'shared' if verdict == 'claimed_shared'
                 else 'granted' if verdict == 'done'
                 else 'refused',
        'seized': ('named' if _verdict_owner(seized)
                   == CLAIM_RECLAIM_FOREIGN
                   else 'fenced' if _fenced(seized) else 'open'),
        'demotion': 'in-place' if demoted else 'held',
        'loss': loss_state,
        'hold': 'refused' if hold_clean else 'breached',
        'release': 'done' if released else 'refused',
        'reclaim': 'reseated' if reclaimed else 'wedged',
        'writes': 'landed' if landed else 'stalled',
        'pair': 'reconverged' if reconverged else 'split'}
    return evidence['digest'], violations, evidence


def scenario_claim_reclaim(ctx):
    """Preempt the deployed pair's field claim through a dedicated
    attachment, hold it through the fenced-write demotion — the
    loss journaled naming the induction token — prove the marked
    ex-owner's bound re-grant refuses the standing different-owner
    claim, then release and assert the unattended re-seat: the
    ex-owner's writes land, it walks back to active, and the pair
    reconverges with no restart; restore the claim state and
    launch roles."""
    case = Case('claim-reclaim',
                'A released preemption re-seats the demoted '
                'owner through the bound fencing-loss reclaim',
                'with the deployed pair settled and tracking, a '
                'dedicated attachment\'s claim_writer preempts '
                'the standing owner\'s claim and holds it until '
                'the owner\'s fenced write demotes it in place — '
                'the loss journaled naming the preempting token; '
                'through the held window the marked ex-owner\'s '
                'bound conditional ensure_writer refuses the '
                'different-owner claim rather than preempting; '
                'after release_writer the bound re-grant re-seats '
                'the claim, the ex-owner\'s writes land again, '
                'and the pair reconverges to exactly one active '
                'plus one tracking standby with no restart and '
                'no foreign write having touched the field; two '
                'consecutive passes produce identical digests '
                'and the launch claim state and roles are '
                'restored')
    try:
        if not ctx.get('plant') or ctx.get('plant_ctl') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no plant endpoint or '
                               'plant_ctl seam — the claim ops and '
                               'field reads cannot run')
        tokens = ctx.get('plant_owner') or {}
        deadline = time.monotonic() + CLAIM_RECLAIM_SETTLE
        active = wait_for(lambda: _settled_active(ctx), deadline)
        if active is None:
            reachable = any(
                _try_role(ctx, ctx[name]) is not None
                for name in ('active', 'standby') if ctx.get(name))
            return case.finish(
                'failed' if reachable else 'inconclusive',
                'claim-reclaim-failed: no peer reports '
                'role=active' if reachable
                else 'the pair is unreachable')
        peer = 'standby' if active == 'active' else 'active'
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=CLAIM_RECLAIM_POLL) is None:
            report = _try_role(ctx, ctx[peer])
            if report is None:
                return case.finish('inconclusive', 'the pair\'s '
                                   'other endpoint never answered '
                                   '/role — the peer-stability '
                                   'half cannot run')
            return case.finish('inconclusive', 'the deployed '
                               'pair never settled to one active '
                               'plus a tracking standby — ' + peer
                               + ' reports '
                               + json.dumps(report.get('role')))
        owner = tokens.get(active)
        if owner is None:
            return case.finish('inconclusive', 'the run pins no '
                               'plant-writer owner token for the '
                               'settled active ' + str(active))
        case.observe('field owner: ' + active + ' (' + ctx[active]
                     + ') under pinned token ' + hex(owner)
                     + '; watching peer ' + peer)

        field_out = _field_out_points(ctx)
        if not field_out:
            return case.finish('inconclusive', 'the simulated '
                               'plant serves no field output to '
                               'watch the owner\'s writes on')
        watch = min(field_out)
        field_in = sorted(_field_inputs(ctx))
        probe_point = field_in[0] if field_in else watch
        case.observe('watching field out point ' + str(watch)
                     + '; foreign write probe on point '
                     + str(probe_point))

        digests = []
        for number in (1, 2):
            digest, violations, evidence = _claim_reclaim_pass(
                ctx, active, peer, owner, watch, probe_point)
            ref = save_evidence(ctx['evidence_dir'],
                                'claim-reclaim-pass-'
                                + str(number) + '.json', evidence)
            case.evidence('file', ref, 'claim-reclaim pass '
                          + str(number) + ' — the induction, the '
                          'demotion, the held window, the reclaim '
                          'watch, and the normalized digest')
            if violations:
                failed = any(name == 'claim-reclaim-failed'
                             for name, _ in violations.values())
                diagnostic = 'claim-reclaim-failed' if failed \
                    else 'claim-reclaim-nondeterministic'
                return case.finish(
                    'failed', diagnostic + ': ' + '; '.join(
                        detail for _, detail in
                        list(violations.values())[:4]))
            digests.append(digest)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', 'claim-reclaim-nondeterministic: the '
                'two passes\' digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two released-preemption passes, identical '
                     'digests')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
