"""The claim_observation acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: The claim-observation case restores what it moves — the
# foreign holds release, the preempted owner re-seats the field
# through its own loss-marked reclaim, and the pair lands back on
# the launch claim state and roles — so it needs no declared window.


# --------------------------------------------------------------------
# The observed-claimant journal contract (WW-LCM-001's field-ownership
# audit trail — the per-revision lane evidence for #987's amendment of
# decision 97's contract): a foreign preempt-and-release a peer only
# ever meets through its *probes* — a claimant handed over between the
# recorded loss and the reclaim — must still leave a trace on that
# peer's journal. Before the amendment the episode absorbed silently:
# `field_claim_lost` fires only on the fenced-write demotion, so a
# claim that changed hands while the demoted ex-owner probed left the
# audit naming a claimant the field no longer stood under. A refused
# conditional grant — the loss-marked peer's bound re-grant probe —
# now queues one `field_claim_observed{point, claimant}` record per
# distinct standing-owner token the refusal names, deduplicated on
# the token so a held claim observed by a hundred probes journals
# once, and seeded by the recorded loss's claimant so the
# `field_claim_lost` attribution never double-records. The leg
# replays the handover: an induction claimer preempts the settled
# owner's claim and holds it — the fenced write demotes the owner in
# place, the journal gains one attributed `field_claim_lost` naming
# the induction token — then a second foreign claimer preempts the
# induction claim while the demoted ex-owner probes, and releases it.
# The owner's served journal must carry exactly one
# `field_claim_observed` naming the second claimer, ordered after the
# loss and before the release-and-reclaim role walk, with repeated
# refused probes journaling nothing further — and the same record
# must land in seq order in the manifest-declared durable journal
# file. The claim ops stay on the raw client — `claim_writer` is an
# op `dcs-plant-ctl` does not expose — while the census and field
# reads ride the shipped tool. The named diagnostics are
# claim-observation-failed and claim-observation-nondeterministic;
# two consecutive passes must produce identical digests.

CLAIM_OBSERVATION_SETTLE = 30    # bound on the pair reporting settled
CLAIM_OBSERVATION_POLL = 0.4     # cadence polling the peers mid-episode
CLAIM_OBSERVATION_ROUNDS = 4     # polls the dedup hold must span —
                                 # the observed count stays put while
                                 # every refused probe re-meets the
                                 # standing foreign claim
CLAIM_OBSERVATION_DEADLINE = 15  # bound on the demotion/observation/
                                 # reclaim waits
CLAIM_OBSERVATION_RESTORE = 10   # grace the finally gives a pending reclaim
# The induction attachment's foreign owner token — the preempting
# claim whose hold fences the owner into the journaled, attributed
# fencing loss.
CLAIM_OBSERVATION_FIRST = 0x7161_2d6f_6273_2d31    # "qa-obs-1"
# The observation attachment's foreign owner token — the claimant
# handed the claim between the recorded loss and the reclaim, which
# the demoted ex-owner only ever meets through its refused probes.
CLAIM_OBSERVATION_FOREIGN = 0x7161_2d6f_6273_2d32  # "qa-obs-2"


def _verdict_owner(response):
    """The owner token a fencing verdict attributes the standing
    claim to — the `owner` field the plant's `fenced` and `io.fenced`
    answers both carry under decision 97's attribution — or None on
    an unfenced answer or a build predating the field."""
    return ((response or {}).get('error') or {}).get('owner')


def _claim_observation_pass(ctx, active, peer, owner, watch):
    """One induction->observation->release pass over the settled pair.

    The induction claimer preempts the field owner and holds through
    the fenced-write demotion — the loss journaled naming the
    induction token — then the observation claimer preempts the
    induction claim while the demoted ex-owner's bound re-grant
    probes refuse it, releases, and the marked reclaim re-seats the
    owner. Returns (digest, violations, evidence): digest is the
    pass's normalized verdict record, identical across clean passes;
    violations is {key: (diagnostic, detail)} in first-seen order."""
    base, peer_base = ctx[active], ctx[peer]
    violations = {}
    evidence = {'watch_point': watch, 'owner': owner,
                'induction': CLAIM_OBSERVATION_FIRST,
                'foreign': CLAIM_OBSERVATION_FOREIGN}

    def note(key, diagnostic, detail):
        violations.setdefault(key, (diagnostic, detail))

    def failed(key, detail):
        note(key, 'claim-observation-failed', detail)

    # The audit positions the episode diffs against: each peer's
    # journal floor, the durable journal file's record count, and
    # the watched field output's stamp.
    floors = {}
    for name in (active, peer):
        _, journal = http_json('GET', ctx[name] + '/journal')
        entries = _journal_list(journal)
        floors[name] = (entries[-1].get('seq') or 0) if entries else 0
    journal_file = ctx['journal_files'][active]
    file_floor = len(_journal_entries(journal_file))
    sample0 = _probe_sample(ctx, watch)
    probe0 = _try_plant(ctx, {'op': 'step', 'dt': 0})
    evidence['baseline'] = {'floors': floors, 'file_floor': file_floor,
                            'field': sample0, 'probe': probe0}
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
                              'loss-attribution contract the '
                              'observation rides on: '
                              + json.dumps(probe0)[:300])
    if _verdict_owner(probe0) != owner:
        raise ConnectionError('the standing claim names a foreign '
                              'token, not the launch owner — the rig '
                              'is not in its launch claim state: '
                              + json.dumps(probe0)[:300])

    # The induction: claim_writer preempts unconditionally and the
    # attachment holds it — the fenced-write demotion the observed
    # handover stages behind.
    induction = _plant_connect(ctx)
    foreign = None
    verdict = seized = verdict2 = seized2 = None
    demoted = released = reclaimed = False
    hold_clean = landed = reconverged = False
    loss_state = observed_state = 'silent'
    last_field = 0
    try:
        claim = _plant_request(induction, {
            'op': 'claim_writer', 'owner': CLAIM_OBSERVATION_FIRST})
        evidence['claim'] = claim
        verdict = claim.get('result')
        if verdict == 'claimed_shared':
            note('claim-shared', 'claim-observation-nondeterministic',
                 'the induction claim joined a live foreign holder '
                 '— a leaked attachment shares the induction token: '
                 + json.dumps(claim)[:200])
        elif verdict != 'done':
            raise ConnectionError('the induction claim was refused — '
                                  'the claim-staging lever is '
                                  'unavailable: '
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
        elif _verdict_owner(seized) != CLAIM_OBSERVATION_FIRST:
            failed('preemption-misnamed', 'the fencing verdict '
                   'attributes the preempted claim to '
                   + str(_verdict_owner(seized))
                   + ', not the induction token: '
                   + json.dumps(seized)[:300])

        # The field freezes at the preemption: the owner's writes
        # fence from here and the foreign attachments never write,
        # so any advance is a foreign write landing.
        anchor = wait_for(lambda: _probe_sample(ctx, watch),
                          time.monotonic() + CLAIM_OBSERVATION_DEADLINE,
                          interval=CLAIM_OBSERVATION_POLL)
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
        deadline = time.monotonic() + CLAIM_OBSERVATION_DEADLINE
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
                time.sleep(CLAIM_OBSERVATION_POLL)
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

        # The journaled loss: exactly one field_claim_lost above the
        # floor — one per held claim — attributed to the induction
        # token the field's own fencing verdict named.
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
                           time.monotonic() + CLAIM_OBSERVATION_DEADLINE,
                           interval=CLAIM_OBSERVATION_POLL) or []
        losses = [(entry.get('event') or {})['field_claim_lost']
                  for entry in entries
                  if 'field_claim_lost' in (entry.get('event') or {})]
        evidence['loss'] = {'entries': entries, 'losses': losses}
        if not losses:
            failed('loss-silent', 'the demotion left no '
                   'field_claim_lost on the owner\'s journal — '
                   'the preemption the observation orders after '
                   'went unrecorded')
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
            elif loss.get('claimant') != CLAIM_OBSERVATION_FIRST:
                loss_state = 'misattributed'
                failed('loss-misattributed', 'the journaled '
                       'fencing loss attributes the takeover to '
                       + str(loss.get('claimant'))
                       + ', not the induction token')
            else:
                loss_state = 'attributed'

        # The handover: the observation claimer preempts the
        # induction claim and holds it — the episode the demoted
        # ex-owner can only meet through its refused conditional
        # re-grant probes, never through a fenced write of its own.
        foreign = _plant_connect(ctx)
        claim2 = _plant_request(foreign, {
            'op': 'claim_writer', 'owner': CLAIM_OBSERVATION_FOREIGN})
        evidence['observed_claim'] = claim2
        verdict2 = claim2.get('result')
        if verdict2 == 'claimed_shared':
            note('observed-claim-shared',
                 'claim-observation-nondeterministic',
                 'the observed claim joined a live foreign holder '
                 '— a leaked attachment shares the observation '
                 'token: ' + json.dumps(claim2)[:200])
        elif verdict2 != 'done':
            raise ConnectionError('the observation claim was refused '
                                  '— the claim-staging lever is '
                                  'unavailable: '
                                  + json.dumps(claim2)[:300])

        seized2 = _try_plant(ctx, {'op': 'step', 'dt': 0})
        evidence['seized2'] = seized2
        if seized2 is None:
            raise ConnectionError('the plant never answered the '
                                  'post-handover fencing probe')
        if not _fenced(seized2):
            failed('handover-open', 'the field accepted a '
                   'third-party mutation under the handed-over '
                   'claim: ' + json.dumps(seized2)[:300])
        elif _verdict_owner(seized2) != CLAIM_OBSERVATION_FOREIGN:
            failed('handover-misnamed', 'the fencing verdict '
                   'attributes the handed-over claim to '
                   + str(_verdict_owner(seized2))
                   + ', not the observation token: '
                   + json.dumps(seized2)[:300])

        # The observation wait: the demoted ex-owner's bound
        # conditional re-grant refuses every scan the foreign claim
        # stands, and the refusal must journal exactly one
        # field_claim_observed naming the standing owner token.
        def observed_journal():
            try:
                _, body = http_json('GET', base + '/journal?since='
                                    + str(floors[active]))
            except Exception:
                return None
            found = _journal_list(body)
            if any('field_claim_observed'
                   in (entry.get('event') or {})
                   for entry in found):
                return found
            return None
        wait_for(observed_journal,
                 time.monotonic() + CLAIM_OBSERVATION_DEADLINE,
                 interval=CLAIM_OBSERVATION_POLL)

        # The dedup hold: across the held rounds every further
        # refused probe must journal nothing — the count stays at
        # one — while the peer stays standby and the standing claim
        # keeps naming the observation token.
        def observations():
            try:
                _, body = http_json('GET', base + '/journal?since='
                                    + str(floors[active]))
            except Exception:
                return None
            return [entry for entry in _journal_list(body)
                    if 'field_claim_observed'
                    in (entry.get('event') or {})]

        observed = observations() or []
        hold_clean = True
        counts = [len(observed)]
        hold = []
        for index in range(CLAIM_OBSERVATION_ROUNDS):
            time.sleep(CLAIM_OBSERVATION_POLL)
            report = _try_role(ctx, base)
            partner = _try_role(ctx, peer_base)
            sample = _probe_sample(ctx, watch)
            probe = _try_plant(ctx, {'op': 'step', 'dt': 0})
            observed = observations() or observed
            counts.append(len(observed))
            row = {'owner': (report or {}).get('role'),
                   'peer': (partner or {}).get('role'),
                   'field_tick': (sample or {}).get('tick'),
                   'probe': _probe_error(probe),
                   'probe_owner': _verdict_owner(probe),
                   'observed': len(observed)}
            hold.append(row)
            if report is None:
                hold_clean = False
                failed('owner-silent', 'the demoted owner\'s '
                       'monitor stopped answering through the '
                       'observation hold')
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
                       'left standby through the observation '
                       'hold: ' + json.dumps(partner)[:300])
            if probe is not None:
                if not _fenced(probe):
                    hold_clean = False
                    failed('hold-open', 'the held foreign claim '
                           'let a third-party mutation through: '
                           + json.dumps(probe)[:300])
                elif _verdict_owner(probe) \
                        != CLAIM_OBSERVATION_FOREIGN:
                    hold_clean = False
                    failed('hold-preempted', 'the standing '
                           'claim moved off the observation '
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
        evidence['hold'] = hold

        # The observation audit: exactly one field_claim_observed
        # naming the standing owner — one per observed token, not
        # per probe — with the induction claimant never repeating
        # (the journaled loss's claimant seeds the dedup).
        named = [body for body in
                 [(entry.get('event') or {})['field_claim_observed']
                  for entry in observed]
                 if isinstance(body, dict)]
        standing = [b for b in named
                    if b.get('claimant') == CLAIM_OBSERVATION_FOREIGN]
        reseeded = [b for b in named
                    if b.get('claimant') == CLAIM_OBSERVATION_FIRST]
        evidence['observed'] = {'entries': observed,
                                'counts': counts}
        if not observed:
            observed_state = 'silent'
            failed('observation-silent', 'the demoted owner probed '
                   'the foreign claim across the whole held window '
                   'and never journaled field_claim_observed — the '
                   'claim-episode-invisible-in-audit gap the '
                   'contract exists to close')
        elif any('claimant' not in b for b in named):
            observed_state = 'unattributed'
            failed('observation-unattributed', 'a journaled '
                   'field_claim_observed names no claimant — the '
                   'rig predates the attribution contract: '
                   + json.dumps(observed)[:300])
        elif reseeded:
            observed_state = 'reseeded'
            failed('observation-reseeded', 'the journal observed '
                   'the induction claimant ' +
                   str(CLAIM_OBSERVATION_FIRST) + ' again — the '
                   'recorded field_claim_lost already attributes '
                   'it, so the seeded dedup must journal nothing '
                   'further for that token')
        elif not standing:
            observed_state = 'misattributed'
            failed('observation-misattributed', 'the journaled '
                   'field_claim_observed names '
                   + str([b.get('claimant') for b in named])
                   + ', not the standing owner '
                   + str(CLAIM_OBSERVATION_FOREIGN))
        elif len(observed) != 1 or len(standing) != 1:
            observed_state = 'duplicated'
            failed('observation-duplicated', 'expected exactly one '
                   'field_claim_observed above the journal floor, '
                   'found ' + str(len(observed)) + ' — the '
                   'contract is one record per observed token, '
                   'not one per refused probe')
        elif counts[-1] != 1 or counts != [1] * len(counts):
            observed_state = 'grew'
            failed('observation-grew', 'the field_claim_observed '
                   'count grew across the dedup hold '
                   + json.dumps(counts) + ' — repeated refused '
                   'probes must journal nothing further for the '
                   'same token')
        else:
            observed_state = 'deduplicated'

        # The release: the observation claimer hands the claim back
        # — the ownerless window the demoted ex-owner's fencing-loss
        # mark reclaims from.
        release = _plant_request(foreign, {'op': 'release_writer'})
        evidence['release'] = release
        released = release.get('result') == 'done'
        if not released:
            raise ConnectionError('the observation claim\'s '
                                  'hand-back was refused: '
                                  + json.dumps(release)[:300])

        # The reclaim watch: the marked ex-owner's bound conditional
        # re-grant lands the first scan the field stands unclaimed —
        # re-seating the claim under its own token and walking
        # standby -> promoting -> active with no operator call and
        # no restart.
        reclaim = {'polls': 0, 'answered': 0,
                   'owner': [], 'peer': []}
        deadline = time.monotonic() + CLAIM_OBSERVATION_DEADLINE
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
                time.sleep(CLAIM_OBSERVATION_POLL)
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
                          time.monotonic() + CLAIM_OBSERVATION_DEADLINE,
                          interval=CLAIM_OBSERVATION_POLL) is not None
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
            time.monotonic() + CLAIM_OBSERVATION_SETTLE,
            interval=CLAIM_OBSERVATION_POLL) is not None
        evidence['reconverged'] = reconverged
        if not reconverged:
            failed('never-reconverged', 'the pair never '
                   'reconverged to ' + active + ' active plus '
                   + peer + ' tracking standby after the '
                   'reclaim')

        # The journal audit above the floor: the episode reads in
        # seq order — the attributed loss, then exactly one
        # observed-claimant record, then the reclaim role walk —
        # while the tracking peer's journal carries no loss and no
        # role change.
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
        seqs = [entry.get('seq') for entry in owner_journal or []]
        if any(not isinstance(seq, int) for seq in seqs) \
                or seqs != sorted(seqs):
            failed('journal-unordered', 'the owner\'s served '
                   'journal is not in seq order above the floor: '
                   + json.dumps(seqs)[:300])
        event_at = {}
        for entry in owner_journal or []:
            event = entry.get('event') or {}
            for kind in event:
                event_at.setdefault(kind, []).append(
                    entry.get('seq'))
        walk = [(change.get('from'), change.get('to'))
                for entry in owner_journal or []
                for change in
                [(entry.get('event') or {}).get('role_changed') or {}]
                if change]
        if reclaimed and ('promoting', 'active') not in walk \
                and ('standby', 'active') not in walk:
            failed('reclaim-unrecorded', 'the unattended '
                   'reclaim left no journaled walk back to '
                   'active — an operator or restart path '
                   'ran instead: ' + json.dumps(walk))
        observed_seqs = event_at.get('field_claim_observed') or []
        lost_seqs = event_at.get('field_claim_lost') or []
        promoting_seqs = [entry.get('seq')
                          for entry in owner_journal or []
                          if (entry.get('event') or {})
                          .get('role_changed')
                          == {'from': 'standby',
                              'to': 'promoting'}]
        if observed_seqs and lost_seqs and promoting_seqs:
            if not lost_seqs[0] < observed_seqs[0] \
                    < promoting_seqs[0]:
                failed('journal-ordering', 'the served journal '
                       'orders the episode wrong — '
                       'field_claim_lost at seq '
                       + str(lost_seqs[0])
                       + ', field_claim_observed at seq '
                       + str(observed_seqs[0])
                       + ', standby->promoting at seq '
                       + str(promoting_seqs[0])
                       + ' — the observation must sit between '
                       'the loss and the reclaim walk')
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

        # The durable file: the manifest-declared journal carries
        # the same record in seq order — the observation the
        # monitor serves is the observation that persists.
        items = _journal_entries(journal_file)[file_floor:]
        file_entries = [item['entry'] for item in items
                        if isinstance(item.get('entry'), dict)
                        and isinstance(
                            item['entry'].get('seq'), int)]
        file_seqs = [entry['seq'] for entry in file_entries]
        if file_seqs != sorted(file_seqs):
            failed('file-unordered', 'the durable journal file '
                   'is not in seq order above the floor: '
                   + json.dumps(file_seqs)[:300])
        file_observed = [entry for entry in file_entries
                         if 'field_claim_observed'
                         in (entry.get('event') or {})]
        file_named = [entry for entry in file_observed
                      if (entry.get('event') or {})
                      .get('field_claim_observed', {})
                      .get('claimant') == CLAIM_OBSERVATION_FOREIGN]
        file_lost = [index for index, entry in
                     enumerate(file_entries)
                     if 'field_claim_lost'
                     in (entry.get('event') or {})]
        file_promoting = [index for index, entry in
                          enumerate(file_entries)
                          if (entry.get('event') or {})
                          .get('role_changed')
                          == {'from': 'standby',
                              'to': 'promoting'}]
        evidence['file'] = {'records': len(items),
                            'observed': file_observed}
        if not file_observed:
            failed('file-observation-missing', 'the durable '
                   'journal file carries no '
                   'field_claim_observed record — the '
                   'observation the monitor serves never '
                   'persisted')
        elif len(file_named) != 1:
            failed('file-observation-count', 'the durable '
                   'journal file carries ' + str(len(file_named))
                   + ' field_claim_observed records naming the '
                   'standing owner — the contract is exactly '
                   'one')
        elif file_lost and file_promoting:
            observed_pos = file_entries.index(file_named[0])
            if not file_lost[0] < observed_pos \
                    < file_promoting[0]:
                failed('file-ordering', 'the durable journal '
                       'file orders the episode wrong — the '
                       'field_claim_observed record must sit '
                       'in seq order between the loss and the '
                       'reclaim walk')
            else:
                evidence['file']['ordered'] = True
    finally:
        induction.close()
        if foreign is not None:
            foreign.close()
        if not released:
            # Best effort: a stranded foreign hold — or a
            # holderless foreign claim standing on the
            # never-released rule — keeps fencing the ex-owner's
            # bound re-grant. Re-claim and release through a
            # fresh attachment so the field stands unclaimed for
            # the reclaim.
            try:
                restore = _plant_connect(ctx)
                try:
                    _plant_request(restore, {'op': 'claim_writer',
                                             'owner':
                                             CLAIM_OBSERVATION_FOREIGN})
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
                 time.monotonic() + CLAIM_OBSERVATION_RESTORE,
                 interval=CLAIM_OBSERVATION_POLL)
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
                   == CLAIM_OBSERVATION_FIRST
                   else 'fenced' if _fenced(seized) else 'open'),
        'demotion': 'in-place' if demoted else 'held',
        'loss': loss_state,
        'handover': 'shared' if verdict2 == 'claimed_shared'
                    else 'granted' if verdict2 == 'done'
                    else 'refused',
        'seized2': ('named' if _verdict_owner(seized2)
                    == CLAIM_OBSERVATION_FOREIGN
                    else 'fenced' if _fenced(seized2) else 'open'),
        'observed': observed_state,
        'hold': 'refused' if hold_clean else 'breached',
        'release': 'done' if released else 'refused',
        'reclaim': 'reseated' if reclaimed else 'wedged',
        'writes': 'landed' if landed else 'stalled',
        'pair': 'reconverged' if reconverged else 'split'}
    return evidence['digest'], violations, evidence


def scenario_claim_observation(ctx):
    """Stage the claim handover #987's contract journals: a dedicated
    attachment's claim_writer preempts the deployed pair's field
    claim and holds through the fenced-write demotion — the loss
    journaled naming the induction token — then a second attachment
    preempts the induction claim while the demoted ex-owner probes
    and releases; assert the owner's journal carries exactly one
    deduplicated field_claim_observed naming the standing owner —
    between the loss and the reclaim walk in seq order, in both the
    served journal and the durable file — the reclaim re-seats the
    owner, and the pair reconverges; restore the launch claim state
    and roles."""
    case = Case('claim-observation',
                'A foreign claim observed only through refused '
                'probes journals exactly one attributed '
                'field_claim_observed record',
                'with the deployed pair settled and tracking, an '
                'induction claim_writer preempts the standing '
                'owner\'s claim and holds through the fenced-write '
                'demotion — the loss journaled naming the induction '
                'token — then a second claim_writer preempts the '
                'induction claim while the demoted ex-owner\'s '
                'bound re-grant probes refuse it, and releases; '
                'the owner\'s journal must carry exactly one '
                'field_claim_observed naming the second token — '
                'deduplicated on the token so repeated probes '
                'journal nothing further, seeded so the recorded '
                'loss\'s claimant never re-observes — ordered '
                'after the attributed loss and before the reclaim '
                'role walk in the served journal and in seq order '
                'in the manifest-declared durable journal file, '
                'while the tracking peer\'s journal carries no '
                'loss and no role change; the released claim '
                're-seats the ex-owner\'s writes and the pair '
                'reconverges to its launch layout; two '
                'consecutive passes produce identical digests '
                'and the launch claim state and roles are '
                'restored')
    try:
        if not ctx.get('plant') or ctx.get('plant_ctl') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no plant endpoint or '
                               'plant_ctl seam — the claim ops and '
                               'field reads cannot run')
        journal_files = ctx.get('journal_files') or {}
        if not journal_files.get('active') \
                or not journal_files.get('standby'):
            return case.finish('inconclusive', 'the run context '
                               'carries no durable journal file '
                               'for the deployed pair — the '
                               'manifest-declared surface the '
                               'observation record must land in '
                               'is unavailable')
        tokens = ctx.get('plant_owner') or {}
        deadline = time.monotonic() + CLAIM_OBSERVATION_SETTLE
        active = wait_for(lambda: _settled_active(ctx), deadline)
        if active is None:
            reachable = any(
                _try_role(ctx, ctx[name]) is not None
                for name in ('active', 'standby') if ctx.get(name))
            return case.finish(
                'failed' if reachable else 'inconclusive',
                'claim-observation-failed: no peer reports '
                'role=active' if reachable
                else 'the pair is unreachable')
        peer = 'standby' if active == 'active' else 'active'
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=CLAIM_OBSERVATION_POLL) is None:
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
                     + '; observing peer ' + peer)

        field_out = _field_out_points(ctx)
        if not field_out:
            return case.finish('inconclusive', 'the simulated '
                               'plant serves no field output to '
                               'watch the owner\'s writes on')
        watch = min(field_out)
        case.observe('watching field out point ' + str(watch))

        digests = []
        for number in (1, 2):
            digest, violations, evidence = \
                _claim_observation_pass(ctx, active, peer, owner,
                                        watch)
            ref = save_evidence(ctx['evidence_dir'],
                                'claim-observation-pass-'
                                + str(number) + '.json', evidence)
            case.evidence('file', ref, 'claim-observation pass '
                          + str(number) + ' — the induction, the '
                          'demotion, the observed handover, the '
                          'dedup hold, the reclaim watch, and the '
                          'normalized digest')
            case.observe('pass ' + str(number) + ': '
                         + json.dumps(digest, sort_keys=True))
            if violations:
                failed = any(name == 'claim-observation-failed'
                             for name, _ in violations.values())
                diagnostic = 'claim-observation-failed' if failed \
                    else 'claim-observation-nondeterministic'
                return case.finish(
                    'failed', diagnostic + ': ' + '; '.join(
                        detail for _, detail in
                        list(violations.values())[:4]))
            digests.append(digest)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', 'claim-observation-nondeterministic: the '
                'two passes\' digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two observed-handover passes, identical '
                     'digests — each journaled exactly one '
                     'attributed field_claim_observed beside the '
                     'unchanged loss and role-change records, in '
                     'the served journal and the durable file')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
