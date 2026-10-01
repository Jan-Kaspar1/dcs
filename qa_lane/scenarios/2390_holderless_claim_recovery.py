"""The holderless_claim_recovery acceptance leg — one module per leg
of the scenario schedule; see qa_lane/scenarios/__init__.py for the
ordering rule and the shared seam."""
from .common import *

# Ordering: the leg restores what it moves — the induction's foreign
# claims release, the recovered pair's launch roles come back through
# the documented promote, and every pass ends on the launch claim
# state — so it needs no declared window.


# --------------------------------------------------------------------
# The holderless-claim reclaim recovery contract (WW-LCM-001's
# continuity clause — the per-revision lane evidence for the contract
# #1255's fix establishes, decision 97's field-arbitrated ownership):
# a standing write claim with zero holders is the dead-owner shape
# `claim_writer_unless_held` exists to preempt, and the fencing-loss
# peer's bound `reclaim_writer` must not refuse "a different owner
# stands" forever against it. The defect reproduced on the shipped
# unkeyed pair: two successive ex-owners orphaned (the launch owner
# demoted by the standby's operator promote; the successor fenced by
# a foreign tool claim), the field's freeing raced both peers'
# unbound `ensure_writer` probes, the winner left a holderless claim
# standing under the cleared peer's stale token, and the loss-marked
# peer's bound reclaim — probing ensure semantics — refused every
# scan while the placeholder fenced the field: the pair lost all
# field ownership until an operator POST /promote.
#
# The leg stages the finding on the deployed simulated rig: POST
# /promote on the tracking standby supersedes the settled owner —
# the first ex-owner, demoted in place, re-joining tracking so its
# loss mark clears; a scenario attachment's `claim_writer` under a
# foreign tool token then fences the promoted owner's write — the
# second ex-owner demoting orphaned with its loss mark standing;
# a second attachment's `claim_writer`/`release_writer` frees the
# field while both unbound probes race, and a staged unbound
# `ensure_writer` raises the cleared ex-owner's holderless claim —
# the wedge shape. The contract: the bound reclaim takes that
# holderless claim — the designated path, whichever ex-owner's
# arm reaches it — with no operator promote and no restart, joining
# the run's attachments so the recovered owner's writes land and the
# pair reconverges to exactly one active plus one tracking standby.
# The orphan budget's own recorded preemption
# (`claim_writer_unless_held` at the miss budget) is the broken
# reclaim's symptom, never an alternative success: a fixed pair
# re-seats the field on its next scan, so the resolution window's
# bound sits under the lane's failover miss budget and that rescue
# can never be what unwedges the placeholder — which is what keeps
# the leg per-revision evidence for the contract rather than for the
# clock.
#
# Every stage audits through both peers' serving monitors and the
# bind-mounted --journal-file mirrors: the attributed
# field_claim_lost on each ex-owner (the promote's successor token
# on the first, the foreign token on the second), the field_orphaned
# transitions, the refused probes' field_claim_observed claimant
# records, and the winner's role_changed walk carrying
# `origin: reclaim` — never `request` — for the unattended recovery.
#
# Named diagnostics: holderless-reclaim-failed tags the contract
# clauses — a demotion that never lands, an unattributed loss, the
# held foreign claim preempted or the window wedging both peers
# active, the release leaving its claim standing, the holderless
# placeholder dropped, the wedge itself (neither peer re-seating,
# the assertable negative: a pair reported recovered while
# probe_writer still names the holderless claim and both peers stay
# orphaned), an operator or failover origin on the recovery walk,
# the recovered owner's writes still fenced, the pair never
# reconverging, the durable mirror absent, the launch roles never
# restoring — and holderless-reclaim-nondeterministic tags the
# instability the contract does not answer for: a refused staging
# lever, a dropped monitor/plant/journal read, the staged unbound
# probe racing a recovery already landed, two passes' digests
# diverging. The unchecked-diagnostic self-check replays the judge
# over planted negatives — a silent audit reports
# holderless-reclaim-unchecked. The leg is inconclusive when the
# staged run predates the contract: no probe_writer claim surface,
# no owner/monitor attribution on the fencing verdicts, no pinned
# owner tokens, no plant_ctl seam, no durable journal mounts, or an
# --auto-promote budget whose own rescue would fire inside the
# resolution watch and stand in for the reclaim it judges.

HOLDERLESS_SETTLE = 30    # bound on each settle watch
HOLDERLESS_POLL = 0.4     # cadence polling the peers mid-episode
HOLDERLESS_DEADLINE = 15  # bound on the demotion/island waits
HOLDERLESS_ROUNDS = 5     # polls the held foreign-claim window spans
HOLDERLESS_RESOLVE = 8    # bound on the unattended reclaim — kept
                          # under the lane's orphan failover miss
                          # budget, so that budget's own
                          # claim_writer_unless_held rescue can never
                          # be what unwedges the placeholder the watch
                          # exists to catch (a rig configured so it
                          # would is reported inconclusive)
HOLDERLESS_RESTORE = 20   # bound on the launch-layout restore
DIAG_FAILED = 'holderless-reclaim-failed'
DIAG_NONDET = 'holderless-reclaim-nondeterministic'
DIAG_UNCHECKED = 'holderless-reclaim-unchecked'

# The induction tokens the scenario's attachments claim under — small
# fixed values colliding with none of the run's pinned controller
# tokens: FOREIGN_1 is the tool claim that fences the promoted owner
# and holds through the held window; FOREIGN_2 is the second tool
# claim whose release frees the field for the raced unbound probes.
HOLDERLESS_FOREIGN_1 = 0x7161_2d68_6c64_2d31  # "qa-hld-1"
HOLDERLESS_FOREIGN_2 = 0x7161_2d68_6c64_2d32  # "qa-hld-2"


def _claim_probe(ctx):
    """The field's standing claim read through the plant protocol —
    `probe_writer`, the non-mutating verdict a foreign attachment's
    mutation would meet."""
    return _try_plant(ctx, {'op': 'probe_writer'})


def _probe_owner(response):
    """The owner token a fenced probe verdict attributes the standing
    claim to — or None on an unfenced/unattributed answer."""
    return ((response or {}).get('error') or {}).get('owner')


def _probe_monitor(response):
    """The declared monitor a fenced probe verdict names (None
    absent)."""
    return ((response or {}).get('error') or {}).get('monitor')


def _sync_kind(report):
    """The standby's served sync kind — tracking / orphaned /
    diverged / unsynchronized — or 'missing' on a bare report."""
    sync = ((report or {}).get('sync') or {})
    for kind in ('tracking', 'orphaned', 'diverged', 'unsynchronized'):
        if kind in sync:
            return kind
    return 'missing'


def _served_journal(ctx, name, floor):
    """The peer's served journal entries since `floor`, or None —
    the dropped cursor and the dropped read land the same: the
    audit that needed them never ran."""
    if floor is None:
        return None
    try:
        _, body = http_json('GET', ctx[name] + '/journal?since='
                            + str(floor))
    except Exception:
        return None
    return _journal_list(body)


def _journaled(ctx, name, floor, kind):
    """The bodies of `kind` events the peer journaled since `floor`."""
    entries = _served_journal(ctx, name, floor)
    if entries is None:
        return None
    return [entry['event'][kind] for entry in entries
            if kind in (entry.get('event') or {})]


def _journaled_roles(ctx, name, floor):
    """The peer's journaled role_changed transitions since `floor`
    as (from, to, origin) tuples — the walk attribution the recovery
    audit reads."""
    entries = _served_journal(ctx, name, floor)
    if entries is None:
        return None
    return [(change.get('from'), change.get('to'),
             change.get('origin'))
            for entry in entries
            for change in
            [(entry.get('event') or {}).get('role_changed') or {}]
            if change]


def _durable_kinds(ctx, name):
    """The event kinds the peer's bind-mounted --journal-file holds,
    or None when the file can't be read."""
    try:
        records = _journal_entries(ctx['journal_files'][name])
    except Exception:
        return None
    kinds = set()
    for record in records:
        event = ((record.get('entry') or {}).get('event')
                 or record.get('run_boundary') or {})
        kinds.update(event)
    return kinds


def _try_checkpoint(ctx, name):
    """GET /checkpoint on a peer, or None when it does not answer."""
    try:
        _, body = http_json('GET', ctx[name] + '/checkpoint')
    except Exception:
        return None
    return body


def _wait_role(ctx, name, role, watch):
    """Poll /role until the peer reports `role`; the poll rows land
    in `watch` for the timeline evidence — each {role, sync,
    answered} so the judge can separate a monitor that stopped
    answering from a walk that never landed."""
    def found():
        report = _try_role(ctx, ctx[name])
        watch.append({'role': (report or {}).get('role'),
                      'sync': _sync_kind(report),
                      'answered': report is not None})
        return report if (report or {}).get('role') == role \
            else None
    return wait_for(found, time.monotonic() + HOLDERLESS_DEADLINE,
                    interval=HOLDERLESS_POLL)


def _wait_tracking(ctx, name, watch):
    """Poll /role until the peer reports standby tracking."""
    def found():
        report = _try_role(ctx, ctx[name])
        watch.append({'role': (report or {}).get('role'),
                      'sync': _sync_kind(report),
                      'answered': report is not None})
        if (report or {}).get('role') == 'standby' \
                and _sync_kind(report) == 'tracking':
            return report
        return None
    return wait_for(found, time.monotonic() + HOLDERLESS_SETTLE,
                    interval=HOLDERLESS_POLL)


def _wait_island(ctx, owner, peer, watch):
    """Poll both /role endpoints until the pair reports the orphan
    island: each peer standby with the orphaned sync — the
    two-ex-owner wedge shape the reclaim contract must close.
    Returns {'owner': report, 'peer': report} or None."""
    def found():
        owner_report = _try_role(ctx, ctx[owner])
        peer_report = _try_role(ctx, ctx[peer])
        watch.append({
            'owner_role': (owner_report or {}).get('role'),
            'owner_sync': _sync_kind(owner_report),
            'owner_answered': owner_report is not None,
            'peer_role': (peer_report or {}).get('role'),
            'peer_sync': _sync_kind(peer_report),
            'peer_answered': peer_report is not None})
        if (owner_report or {}).get('role') == 'standby' \
                and _sync_kind(owner_report) == 'orphaned' \
                and (peer_report or {}).get('role') == 'standby' \
                and _sync_kind(peer_report) == 'orphaned':
            return {'owner': owner_report, 'peer': peer_report}
        return None
    return wait_for(found, time.monotonic() + HOLDERLESS_DEADLINE,
                    interval=HOLDERLESS_POLL)


def _holderless_restore(ctx, owner, peer):
    """Best-effort launch-layout restore: demote whichever peer still
    owns the field, promote the launch owner back over it, and let
    the pair reconverge. Every step is retried inside the bound and
    swallowed on refusal — cleanup, never the recovery the leg
    judges."""
    try:
        report = _try_role(ctx, ctx[peer])
        if (report or {}).get('role') in ('active', 'promoting'):
            _settle_call(ctx[peer] + '/demote')
            _wait_role(ctx, peer, 'standby', [])
        deadline = time.monotonic() + HOLDERLESS_RESTORE
        while time.monotonic() < deadline:
            if (_try_role(ctx, ctx[owner]) or {}).get('role') \
                    != 'active':
                _settle_call(ctx[owner] + '/promote')
            if _pair_active(ctx) == owner \
                    and _tracking_standby(ctx, peer) is not None:
                return
            time.sleep(HOLDERLESS_POLL)
    except Exception:
        pass


def _holderless_pass(ctx, number, owner, peer, tokens, field_point):
    """One induction/recovery pass: the tracking standby's promote
    supersedes the launch owner (the first ex-owner), the foreign
    tool claim fences the promoted peer (the second ex-owner,
    orphaned with its loss mark standing), the second attachment's
    claim and release free the field while a staged unbound
    ensure_writer raises the cleared ex-owner's holderless claim,
    and the resolution watch proves the bound reclaim — never an
    operator call — re-seats the field. Returns (record, evidence):
    the record is what the judge replays; an aborted stage simply
    leaves its later keys absent for the judge to name."""
    record = {'owner_token': tokens[owner],
              'peer_token': tokens[peer]}
    evidence = {'pass': number, 'owner': owner, 'peer': peer}
    floors = {}
    for name in (owner, peer):
        try:
            floors[name] = _journal_cursor(ctx, ctx[name])
        except Exception:
            floors[name] = None
    evidence['floors'] = floors
    attachments = []
    try:
        # Stage A — the first ex-owner: the tracking standby's
        # operator promote supersedes the settled owner, which
        # demotes in place and re-joins tracking so its loss mark
        # clears — the reproduction's cleared peer.
        status, body = _settle_call(ctx[peer] + '/promote')
        record['promote'] = {'status': status, 'body': body}
        evidence['promote'] = record['promote']
        if status != 200:
            return record, evidence
        demote_rows = []
        walked = _wait_role(ctx, owner, 'standby', demote_rows)
        evidence['demotion'] = demote_rows[-8:]
        record['demoted'] = {
            'walked': walked is not None,
            'walk': [row.get('role') for row in demote_rows],
            'unanswered': sum(1 for row in demote_rows
                              if not row['answered'])}
        if walked is None:
            return record, evidence
        track_rows = []
        tracked = _wait_tracking(ctx, owner, track_rows)
        evidence['rejoin'] = track_rows[-8:]
        record['rejoined'] = {
            'tracked': tracked is not None,
            'unanswered': sum(1 for row in track_rows
                              if not row['answered'])}
        record['owner_loss'] = _journaled(
            ctx, owner, floors[owner], 'field_claim_lost')
        evidence['owner_loss'] = record['owner_loss']
        record['seated'] = _claim_probe(ctx)
        evidence['seated'] = record['seated']
        if tracked is None:
            return record, evidence

        # Stage B — the second ex-owner: a foreign tool claim
        # preempts the promoted owner; its first fenced write
        # demotes it orphaned with the loss mark standing.
        first = _plant_connect(ctx)
        attachments.append(first)
        claim1 = _plant_request(first, {
            'op': 'claim_writer', 'owner': HOLDERLESS_FOREIGN_1,
            'controller': False})
        record['claim1'] = claim1
        evidence['claim1'] = claim1
        if claim1.get('result') not in ('done', 'claimed_shared'):
            raise ConnectionError('the induction claim was '
                                  'refused — the claim-staging '
                                  'lever is absent: '
                                  + json.dumps(claim1)[:300])
        record['seized'] = _claim_probe(ctx)
        evidence['seized'] = record['seized']
        fenced_rows = []
        fenced = _wait_role(ctx, peer, 'standby', fenced_rows)
        evidence['fenced'] = fenced_rows[-8:]
        record['peer_demoted'] = {
            'walked': fenced is not None,
            'walk': [row.get('role') for row in fenced_rows],
            'unanswered': sum(1 for row in fenced_rows
                              if not row['answered'])}
        if fenced is None:
            return record, evidence
        record['marked_loss'] = _journaled(
            ctx, peer, floors[peer], 'field_claim_lost')
        evidence['marked_loss'] = record['marked_loss']

        # The orphan island: both ex-owners standby with the
        # orphaned sync — the field serves no owner while the
        # foreign tool claim holds.
        island_rows = []
        island = _wait_island(ctx, owner, peer, island_rows)
        evidence['island'] = island_rows[-8:]
        record['island'] = island
        if island is None:
            return record, evidence

        # The held window: the live foreign claim must refuse every
        # conditional path — both peers stay standby-orphaned and
        # the probe keeps naming the tool token.
        hold = []
        for index in range(HOLDERLESS_ROUNDS):
            probe = _claim_probe(ctx)
            peer_report = _try_role(ctx, ctx[peer])
            owner_report = _try_role(ctx, ctx[owner])
            hold.append({
                'owner_role': (owner_report or {}).get('role'),
                'owner_sync': _sync_kind(owner_report),
                'peer_role': (peer_report or {}).get('role'),
                'peer_sync': _sync_kind(peer_report),
                'probe': _probe_error(probe),
                'probe_owner': _probe_owner(probe)})
            if index + 1 < HOLDERLESS_ROUNDS:
                time.sleep(HOLDERLESS_POLL)
        record['hold'] = hold
        evidence['hold'] = hold

        # Stage C — the freeing: the second attachment's
        # claim_writer preempts the first tool claim and its
        # release_writer leaves the field unclaimed; the staged
        # unbound ensure_writer — the orphan cycle's probe shape —
        # raises the cleared ex-owner's holderless placeholder, the
        # winner's stale token the raced probes left standing.
        second = _plant_connect(ctx)
        attachments.append(second)
        claim2 = _plant_request(second, {
            'op': 'claim_writer', 'owner': HOLDERLESS_FOREIGN_2,
            'controller': False})
        record['claim2'] = claim2
        evidence['claim2'] = claim2
        if claim2.get('result') not in ('done', 'claimed_shared'):
            raise ConnectionError('the freeing claim was refused: '
                                  + json.dumps(claim2)[:300])
        release = _plant_request(second, {'op': 'release_writer'})
        record['release'] = release
        evidence['release'] = release
        if release.get('result') != 'done':
            raise ConnectionError('the freeing release was '
                                  'refused: '
                                  + json.dumps(release)[:300])
        record['freed'] = _claim_probe(ctx)
        evidence['freed'] = record['freed']
        staged = _plant_request(second, {
            'op': 'ensure_writer', 'owner': tokens[owner],
            'rebind': False, 'controller': True})
        record['staged'] = staged
        evidence['staged'] = staged

        # The holderless placeholder standing: probe first (the
        # marked peer's own /role poll lands its reclaim scan
        # first), then read both roles — a named claim whose owner
        # reports standby is the dead-owner shape.
        verdict = _claim_probe(ctx)
        peer_report = _try_role(ctx, ctx[peer])
        owner_report = _try_role(ctx, ctx[owner])
        named = _probe_owner(verdict)
        named_role = None
        if named == tokens[owner]:
            named_role = (owner_report or {}).get('role')
        elif named == tokens[peer]:
            named_role = (peer_report or {}).get('role')
        record['placeholder'] = {
            'verdict': verdict, 'named': named,
            'named_role': named_role,
            'standing': _fenced(verdict) and (
                named not in (tokens[owner], tokens[peer])
                or named_role in ('standby', 'demoting'))}
        evidence['placeholder'] = record['placeholder']

        # The field tick freezes through the orphan island — every
        # write is fenced until a bound reclaim re-seats it — so the
        # recovery's writes landing is the bound-grant proof.
        anchor = (_probe_sample(ctx, field_point) or {}).get('tick')

        # Stage D — the resolution watch: no operator call, no
        # restart — the loss-marked peer's bound reclaim or the
        # recorded holderless preemption must re-seat the field
        # inside the bound.
        rows = []
        winner = None
        deadline = time.monotonic() + HOLDERLESS_RESOLVE
        while time.monotonic() < deadline and winner is None:
            probe = _claim_probe(ctx)
            peer_report = _try_role(ctx, ctx[peer])
            owner_report = _try_role(ctx, ctx[owner])
            rows.append({
                'peer_role': (peer_report or {}).get('role'),
                'peer_sync': _sync_kind(peer_report),
                'peer_answered': peer_report is not None,
                'owner_role': (owner_report or {}).get('role'),
                'owner_sync': _sync_kind(owner_report),
                'owner_answered': owner_report is not None,
                'probe': _probe_error(probe),
                'probe_owner': _probe_owner(probe)})
            for name, report in ((peer, peer_report),
                                 (owner, owner_report)):
                if (report or {}).get('role') == 'active':
                    winner = name
            if winner is None:
                time.sleep(HOLDERLESS_POLL)
        evidence['resolution'] = rows[-12:]
        record['resolution'] = {
            'winner': winner,
            'winner_side': (None if winner is None
                            else 'peer' if winner == peer
                            else 'owner'),
            'winner_token': (None if winner is None
                             else tokens[winner]),
            'rows': rows, 'operator': False}

        # The winner's journaled walk — the unattended recovery's
        # attribution — and the loser's quiet journal; then the
        # reconvergence, the bound-grant write proof, and the
        # standing claim naming the winner's token.
        loser = peer if winner == owner else owner
        record['walk'] = _journaled_roles(ctx, winner, floors[winner]) \
            if winner is not None else None
        record['loser_moves'] = _journaled_roles(ctx, loser,
                                                 floors[loser])
        record['observed'] = {
            side: (None if bodies is None
                   else [body.get('claimant') for body in bodies])
            for side, bodies in
            ((side, _journaled(ctx, name, floors[name],
                               'field_claim_observed'))
             for side, name
             in (('owner', owner), ('peer', peer)))}
        evidence['walk'] = record['walk']
        evidence['loser_moves'] = record['loser_moves']
        evidence['observed'] = record['observed']
        if winner is None:
            return record, evidence
        record['reconverged'] = wait_for(
            lambda: _tracking_standby(ctx, loser),
            time.monotonic() + HOLDERLESS_DEADLINE,
            interval=HOLDERLESS_POLL) is not None

        def advancing():
            _try_role(ctx, ctx[winner])   # keep the scans driving
            sample = _probe_sample(ctx, field_point)
            if isinstance(anchor, int) and isinstance(
                    (sample or {}).get('tick'), int) \
                    and sample['tick'] > anchor:
                return sample
            return None

        landed = wait_for(advancing,
                          time.monotonic() + HOLDERLESS_DEADLINE,
                          interval=HOLDERLESS_POLL)
        record['writes'] = {'anchor': anchor,
                            'landed': landed is not None}
        record['final_probe'] = _claim_probe(ctx)
        evidence['final_probe'] = record['final_probe']
        record['durable'] = {
            side: (sorted(kinds) if kinds is not None else None)
            for side, kinds in
            ((side, _durable_kinds(ctx, name))
             for side, name
             in (('owner', owner), ('peer', peer)))}
        evidence['durable'] = record['durable']
    finally:
        for conn in attachments:
            try:
                conn.close()
            except Exception:
                pass
        # The launch roles for the next pass and the legs behind —
        # the documented operator promote is the restore's lever,
        # never the recovery's.
        _holderless_restore(ctx, owner, peer)
        record['restored'] = (
            _pair_active(ctx) == owner
            and _tracking_standby(ctx, peer) is not None)
        verdict = _claim_probe(ctx)
        record['restored_owner'] = _probe_owner(verdict) \
            if isinstance(verdict, dict) else None
    return record, evidence


def _judge_holderless(record, note):
    """Audit one pass's record — replayable, so the self-check can
    hand it planted negatives. `note(key, diagnostic, detail)`
    records each clause the record violates: DIAG_FAILED tags the
    recovery contract clauses — the demotions landing, the
    attributed losses, the held claim refusing the conditional
    probes, the placeholder standing holderless, the reclaim-origin
    recovery inside the bound, the bound re-seat's writes landing,
    the pair reconverging, the durable mirror, the launch roles
    restoring — and DIAG_NONDET tags the instability the contract
    does not answer for: refused staging levers, dropped reads, the
    staged unbound probe racing an already-landed recovery. An
    aborted stage ends the audit where the pass ended — the later
    keys it never wrote are not clauses."""
    def failed(key, detail):
        note(key, DIAG_FAILED, detail)

    def nondet(key, detail):
        note(key, DIAG_NONDET, detail)

    def loss_clause(key, bodies, claimant):
        """The attributed field_claim_lost audit for one ex-owner:
        exactly one record above the floor naming the preempting
        claimant."""
        if bodies is None:
            nondet(key, 'the served-journal read dropped — the '
                   'loss audit never landed')
            return
        if not bodies:
            failed(key, 'the demoted peer journaled no '
                   'field_claim_lost — the supersede went '
                   'unrecorded')
            return
        if len(bodies) != 1:
            failed(key, 'the demoted peer journaled '
                   + str(len(bodies)) + ' field_claim_lost records '
                   'for one supersede')
            return
        named = (bodies[0] or {}).get('claimant')
        if named is None:
            failed(key, 'the journaled field_claim_lost names no '
                   'claimant: ' + json.dumps(bodies)[:200])
        elif named != claimant:
            failed(key, 'the journaled field_claim_lost names '
                   + str(named) + ' — the claimant '
                   + str(claimant) + ' was expected')

    # Stage A — the promote-staged first ex-owner.
    promote = record.get('promote') or {}
    if promote.get('status') != 200:
        nondet('promote', 'the standby\'s /promote answered '
               + str(promote.get('status')) + ' '
               + json.dumps(promote.get('body'))[:160]
               + ' — the staging call never landed')
        return
    demoted = record.get('demoted') or {}
    if demoted.get('unanswered'):
        failed('monitor', 'the superseded owner\'s monitor dropped '
               + str(demoted['unanswered']) + ' /role reads inside '
               'the demotion watch — a restart shape, not a '
               'demote-in-place')
    if not demoted.get('walked'):
        failed('demotion', 'the superseded owner never reported '
               'standby — the first ex-owner never formed: '
               + json.dumps(demoted.get('walk'))[:200])
        return
    rejoined = record.get('rejoined') or {}
    if rejoined.get('unanswered'):
        failed('monitor', 'the demoted owner\'s monitor dropped '
               + str(rejoined['unanswered']) + ' /role reads inside '
               'the re-join watch')
    if not rejoined.get('tracked'):
        failed('rejoin', 'the first ex-owner never reconverged to a '
               'tracking standby — the cleared-peer shape the '
               'finding stages never formed')
    if 'owner_loss' in record:
        loss_clause('owner-loss', record.get('owner_loss'),
                    record.get('peer_token'))
    seated = record.get('seated')
    if 'seated' in record:
        if not isinstance(seated, dict):
            nondet('seated', 'the claim-surface read dropped — the '
                   'successor\'s claim never landed')
        elif not _fenced(seated):
            failed('seated', 'the promoted peer\'s claim did not '
                   'fence a third-party probe: '
                   + json.dumps(seated)[:200])
        elif _probe_owner(seated) != record.get('peer_token'):
            failed('seated', 'the standing claim names '
                   + str(_probe_owner(seated)) + ' — the promoted '
                   'peer\'s token ' + str(record.get('peer_token'))
                   + ' was expected')

    # Stage B — the foreign tool claim's fenced demotion.
    if 'claim1' in record:
        if (record.get('claim1') or {}).get('result') \
                == 'claimed_shared':
            nondet('claim1', 'the induction claim joined a live '
                   'foreign holder — a leaked attachment shares the '
                   'token: ' + json.dumps(record['claim1'])[:200])
    seized = record.get('seized')
    if 'seized' in record:
        if not isinstance(seized, dict):
            nondet('seized', 'the claim-surface read dropped — the '
                   'preemption never landed')
        elif not _fenced(seized) \
                or _probe_owner(seized) != HOLDERLESS_FOREIGN_1:
            failed('seized', 'the foreign tool claim\'s fencing '
                   'verdict names ' + str(_probe_owner(seized))
                   + ' — the induction token '
                   + str(HOLDERLESS_FOREIGN_1) + ' was expected: '
                   + json.dumps(seized)[:200])
    peer_demoted = record.get('peer_demoted') or {}
    if 'peer_demoted' in record:
        if peer_demoted.get('unanswered'):
            failed('monitor', 'the fenced peer\'s monitor dropped '
                   + str(peer_demoted['unanswered']) + ' /role '
                   'reads inside the demotion watch')
        if not peer_demoted.get('walked'):
            failed('peer-demote', 'the promoted peer\'s fenced '
                   'write never demoted it — the second ex-owner '
                   'never formed: '
                   + json.dumps(peer_demoted.get('walk'))[:200])
            return
    if 'marked_loss' in record:
        loss_clause('marked-loss', record.get('marked_loss'),
                    HOLDERLESS_FOREIGN_1)

    # The orphan island and the held window.
    island = record.get('island')
    if 'island' in record:
        if not isinstance(island, dict):
            failed('island', 'the pair never reported the '
                   'orphaned island — two standby ex-owners each '
                   'pulling an ownerless line')
    hold = record.get('hold')
    if hold is not None:
        if not hold:
            nondet('held', 'the held-window watch recorded no '
                   'rows — the polls never landed')
        for index, row in enumerate(hold):
            if row.get('owner_role') is None \
                    or row.get('peer_role') is None:
                failed('monitor', 'a peer\'s monitor dropped a '
                       '/role read inside the held window — a '
                       'restart shape the reclaim must never need: '
                       + json.dumps(row)[:200])
                continue
            for side, role in (('owner', row.get('owner_role')),
                               ('peer', row.get('peer_role'))):
                if role != 'standby':
                    failed('held', 'the ' + side + ' reported '
                           + str(role) + ' while the foreign claim '
                           'held — a conditional path took what it '
                           'must refuse: row ' + str(index))
            for side, sync in (('owner', row.get('owner_sync')),
                               ('peer', row.get('peer_sync'))):
                if sync != 'orphaned':
                    failed('held', 'the ' + side + ' reported sync '
                           + str(sync) + ' under the held foreign '
                           'claim — the orphan island broke: row '
                           + str(index))
            if row.get('probe') is not None:
                if row['probe'] != 'fenced' \
                        or row.get('probe_owner') \
                        != HOLDERLESS_FOREIGN_1:
                    failed('held', 'the held foreign claim moved '
                           'off the induction token — a conditional '
                           'grant preempted a live holder: row '
                           + str(index) + ' '
                           + json.dumps(row)[:200])

    # Stage C — the free and the staged holderless placeholder.
    if 'claim2' in record:
        if (record.get('claim2') or {}).get('result') \
                == 'claimed_shared':
            nondet('claim2', 'the freeing claim joined a live '
                   'foreign holder: '
                   + json.dumps(record['claim2'])[:200])
    freed = record.get('freed')
    if 'freed' in record:
        if not isinstance(freed, dict):
            nondet('freed', 'the claim-surface read dropped — the '
                   'free never landed')
        elif _fenced(freed) \
                and _probe_owner(freed) == HOLDERLESS_FOREIGN_2:
            failed('freed', 'the released claim still stands '
                   'fencing the field — the freeing release never '
                   'freed it: ' + json.dumps(freed)[:200])
    placeholder = record.get('placeholder') or {}
    if 'placeholder' in record:
        staged_done = (record.get('staged') or {}).get('result') \
            in ('done', 'claimed_shared')
        verdict = placeholder.get('verdict')
        if not isinstance(verdict, dict):
            nondet('placeholder', 'the claim-surface read dropped '
                   '— the holderless placeholder never landed')
        elif not _fenced(verdict):
            if staged_done:
                failed('placeholder', 'the staged unbound probe '
                       'landed the placeholder but the claim read '
                       'answers unfenced — the holderless claim '
                       'never stood: ' + json.dumps(verdict)[:200])
            else:
                nondet('placeholder', 'the freed field re-seated '
                       'or stayed open before the staged unbound '
                       'probe could land: '
                       + json.dumps(verdict)[:200])
        elif not staged_done:
            nondet('placeholder', 'a raced probe or recovery beat '
                   'the staged unbound grant — the holderless stage '
                   'never landed deterministically')
        elif not placeholder.get('standing'):
            nondet('placeholder', 'the freed field\'s claim read '
                   'names a live peer before the holderless stage '
                   'could be observed — the recovery raced past the '
                   'staging: ' + json.dumps(placeholder)[:200])

    # Stage D — the unattended recovery.
    resolution = record.get('resolution') or {}
    rows = resolution.get('rows') or []
    winner = resolution.get('winner')
    if 'resolution' in record:
        unanswered = sum(1 for row in rows
                         if not row.get('peer_answered')
                         or not row.get('owner_answered'))
        if unanswered:
            failed('monitor', 'a peer\'s monitor dropped '
                   + str(unanswered) + ' /role reads inside the '
                   'resolution watch — a restart recovered what the '
                   'bound reclaim must carry')
        if any(row.get('peer_role') == 'active'
               and row.get('owner_role') == 'active'
               for row in rows):
            failed('dual', 'both peers reported role=active in one '
                   'watch row — two field owners stood at once')
        if winner is None:
            failed('resolution', 'the wedge held — neither '
                   'ex-owner\'s bound reclaim took the holderless '
                   'claim inside the bound; the field stood '
                   'ownerless for the whole window: '
                   + json.dumps(rows[-4:])[:400])
        elif rows and rows[-1].get(
                (resolution.get('winner_side') or 'peer')
                + '_role') != 'active':
            failed('reseat', 'the recovered peer did not hold '
                   'active — the re-seat never bound (an unbound '
                   're-take fences the winner\'s own write)')
    walk = record.get('walk')
    loser_moves = record.get('loser_moves')
    if 'walk' in record:
        if walk is None:
            nondet('walk', 'the winner\'s served-journal read '
                   'dropped — the recovery\'s attribution never '
                   'landed')
        else:
            # The recovery's promotion is the walk's LAST
            # standby/demoting -> promoting transition — the same
            # pass's earlier request-origin promotes (the staging
            # lever's own) precede it and stay out of the verdict.
            promoted = [origin for (source, target, origin) in walk
                        if target == 'promoting'
                        and source in ('standby', 'demoting')]
            settled = [origin for (source, target, origin) in walk
                       if (source, target) == ('promoting', 'active')]
            if not promoted:
                failed('walk', 'the winner\'s journal carries no '
                       'standby -> promoting transition — an '
                       'operator or restart path recovered what '
                       'the reclaim must: ' + json.dumps(walk)[:300])
            elif promoted[-1] == 'request':
                failed('walk', 'the recovery walk journals '
                       'origin=request — an operator promote ran: '
                       + json.dumps(walk)[:300])
            elif promoted[-1] is None:
                nondet('walk', 'the recovery walk carries no '
                       'origin field — the rig predates the switch '
                       'attribution contract')
            elif promoted[-1] != 'reclaim':
                failed('walk', 'the recovery walk journals origin '
                       + json.dumps(promoted[-1:])[:120] + ' — the '
                       'holderless placeholder wedged the designated '
                       'path and this origin unwedged it instead of '
                       'the bound fencing-loss reclaim the contract '
                       'names (an orphan failover\'s preempt is the '
                       'budget rescue, not the designated path)')
            elif not settled:
                failed('walk', 'the winner\'s journal never '
                       'settled promoting -> active: '
                       + json.dumps(walk)[:300])
    if 'loser_moves' in record:
        if loser_moves is None:
            nondet('loser-quiet', 'the loser\'s served-journal '
                   'read dropped — the quiet-side audit never '
                   'landed')
        else:
            moved = [(source, target, origin)
                     for source, target, origin in loser_moves
                     if target in ('promoting', 'active')
                     and origin in ('reclaim', 'failover')]
            if moved:
                failed('loser-quiet', 'the losing peer journaled '
                       'its own unattended promotion '
                       + json.dumps(moved)[:200] + ' — two '
                       'recoveries raced the holderless claim')
            elif any(origin is None and target in ('promoting',
                                                   'active')
                     for source, target, origin in loser_moves):
                nondet('loser-quiet', 'the loser\'s walk carries '
                       'no origin field — the attribution audit '
                       'cannot prove it quiet')
    if winner is not None:
        if not record.get('reconverged'):
            failed('reconverge', 'the losing peer never '
                   'reconverged to a tracking standby behind the '
                   'recovered owner')
        writes = record.get('writes') or {}
        if not writes.get('landed'):
            failed('writes', 'the recovered owner\'s writes never '
                   'landed — the reclaim never joined the run\'s '
                   'attachments to the claim (anchor '
                   + str(writes.get('anchor')) + ')')
        final = record.get('final_probe')
        if final is None:
            nondet('reseat-probe', 'the post-recovery claim-surface '
                   'read dropped')
        elif not _fenced(final):
            failed('reseat', 'the recovered claim does not fence '
                   'third-party mutations: '
                   + json.dumps(final)[:200])
        elif _probe_owner(final) != resolution.get('winner_token'):
            failed('reseat', 'the recovered claim names '
                   + str(_probe_owner(final)) + ' — the winner\'s '
                   'token ' + str(resolution.get('winner_token'))
                   + ' was expected')
    observed = record.get('observed') or {}
    if 'observed' in record:
        for name in ('owner', 'peer'):
            if observed.get(name) is None:
                nondet('observed', 'the ' + name + ' peer\'s '
                       'served-journal read dropped — the refused-'
                       'probe audit never landed')
        # The refused-probe audit is the observation epoch's: the
        # fenced peer's own field_claim_lost already attributed the
        # held claimant and seeded its dedup, so the record naming
        # that token must come from the peer that only ever probed it
        # — and the fenced side must not repeat the attribution it
        # already holds. Which side is which is the record's own loss
        # audit; where no loss record names the claimant the pair's
        # union must name it once.
        fenced = None
        for name, bodies in (('owner', record.get('owner_loss')),
                             ('peer', record.get('marked_loss'))):
            if bodies and any(
                    (body or {}).get('claimant') == HOLDERLESS_FOREIGN_1
                    for body in bodies):
                fenced = name
        named = [name for name in ('owner', 'peer')
                 if HOLDERLESS_FOREIGN_1 in (observed.get(name) or [])]
        if fenced is not None and fenced in named:
            failed('observed', 'the ' + fenced + ' peer journaled '
                   'field_claim_observed naming the held foreign '
                   'claimant its own field_claim_lost already '
                   'attributed — one episode recorded twice')
        probed = None if fenced is None else (
            'peer' if fenced == 'owner' else 'owner')
        if probed is not None and probed not in named:
            failed('observed', 'the ' + probed + ' peer journaled no '
                   'field_claim_observed naming the held foreign '
                   'claimant — the refused probes went unrecorded: '
                   + json.dumps(observed)[:200])
        elif probed is None and not named:
            failed('observed', 'neither peer journaled a '
                   'field_claim_observed naming the held foreign '
                   'claimant — the episode went unrecorded: '
                   + json.dumps(observed)[:200])
    durable = record.get('durable') or {}
    if 'durable' in record:
        for name in ('owner', 'peer'):
            kinds = durable.get(name)
            if kinds is None:
                nondet('durable', 'the ' + name + ' peer\'s durable '
                       'journal-file read dropped')
            else:
                for required in ('field_claim_lost',
                                 'field_orphaned',
                                 'field_claim_observed',
                                 'role_changed'):
                    if required not in kinds:
                        failed('durable', 'the ' + name + ' peer\'s '
                               '--journal-file carries no '
                               + required + ' record — the durable '
                               'mirror dropped it: '
                               + json.dumps(kinds)[:200])
    if 'restored' in record:
        if not record.get('restored'):
            failed('restore', 'the pair never settled back to its '
                   'launch roles')
        elif record.get('restored_owner') \
                != record.get('owner_token'):
            failed('restore', 'the restored field claim names '
                   + str(record.get('restored_owner'))
                   + ' — the launch owner\'s token '
                   + str(record.get('owner_token')) + ' was '
                   'expected')


def _holderless_digest(record, violations):
    """The pass's normalized verdict record — identical across clean
    passes; each field is the clean value only while no violation —
    contract or instability — names its clause."""
    def clean(*keys):
        return not any(key in violations for key in keys)
    return {
        'switch': 'tracked' if clean('promote', 'demotion',
                                     'rejoin', 'owner-loss',
                                     'seated') else 'staged',
        'preempt': 'seized' if clean('claim1', 'seized',
                                     'peer-demote', 'marked-loss')
                   else 'open',
        'island': 'formed' if clean('island') else 'absent',
        'held': 'refused' if clean('held') else 'breached',
        'freed': 'released' if clean('freed', 'claim2') else 'held',
        'placeholder': 'standing' if clean('placeholder')
                       else 'dropped',
        'recovery': 'reclaim' if clean('resolution', 'walk',
                                       'loser-quiet', 'monitor', 'dual')
                    else 'other',
        'pair': 'reconverged' if clean('reconverge') else 'split',
        'writes': 'landed' if clean('writes', 'reseat',
                                    'reseat-probe') else 'stalled',
        'journals': 'recorded' if clean('observed', 'durable')
                    else 'absent',
        'restore': 'restored' if clean('restore') else 'unrestored'}


def _holderless_self_check():
    """The leg's unchecked-diagnostic self-test: replay the judge
    over each planted negative — the issue's named doctored record
    (the pair asserted recovered while probe_writer still names a
    holderless claim and both peers stay orphaned), the wedge, an
    operator-origin recovery, a held-window breach, a monitor that
    drops, the silent and misattributed losses, the unrestored pair
    — and require the judge to note each. A silent judge returns
    the negative names it let through."""
    foreign_1, foreign_2 = HOLDERLESS_FOREIGN_1, HOLDERLESS_FOREIGN_2
    owner_token, peer_token = 424243, 424244

    def fenced(owner):
        return {'result': 'error',
                'error': {'kind': 'fenced', 'detail': 'held',
                          'owner': owner}}

    def standby(sync='orphaned'):
        return {'role': 'standby', 'sync': {sync: {}},
                'answered': True}

    slipped = []

    def clean_record():
        return {
            'owner_token': owner_token, 'peer_token': peer_token,
            'promote': {'status': 200, 'body': {'role': 'promoting'}},
            'demoted': {'walked': True,
                        'walk': ['active', 'demoting', 'standby'],
                        'unanswered': 0},
            'rejoined': {'tracked': True, 'unanswered': 0},
            'owner_loss': [{'point': 100, 'claimant': peer_token}],
            'seated': fenced(peer_token),
            'claim1': {'result': 'done'},
            'seized': fenced(foreign_1),
            'peer_demoted': {'walked': True,
                             'walk': ['active', 'demoting',
                                      'standby'],
                             'unanswered': 0},
            'marked_loss': [{'point': 100, 'claimant': foreign_1}],
            'island': {'owner': standby(), 'peer': standby()},
            'hold': [{'owner_role': 'standby',
                      'owner_sync': 'orphaned',
                      'peer_role': 'standby',
                      'peer_sync': 'orphaned',
                      'probe': 'fenced',
                      'probe_owner': foreign_1}] * 3,
            'claim2': {'result': 'done'},
            'release': {'result': 'done'},
            'freed': {'result': 'error',
                      'error': {'kind': 'unclaimed',
                                'detail': 'open'}},
            'staged': {'result': 'done'},
            'placeholder': {'verdict': fenced(owner_token),
                            'named': owner_token,
                            'named_role': 'standby',
                            'standing': True},
            'resolution': {
                'winner': 'standby',
                'operator': False,
                'rows': [
                    {'peer_role': 'promoting',
                     'peer_sync': 'unsynchronized',
                     'peer_answered': True,
                     'owner_role': 'standby',
                     'owner_sync': 'tracking',
                     'owner_answered': True,
                     'probe': 'fenced',
                     'probe_owner': peer_token},
                    {'peer_role': 'active',
                     'peer_sync': 'unsynchronized',
                     'peer_answered': True,
                     'owner_role': 'standby',
                     'owner_sync': 'tracking',
                     'owner_answered': True,
                     'probe': 'fenced',
                     'probe_owner': peer_token}]},
            'walk': [('standby', 'promoting', 'reclaim'),
                     ('promoting', 'active', 'reclaim')],
            'loser_moves': [('active', 'demoting', 'fenced'),
                            ('demoting', 'standby', 'fenced')],
            'observed': {'owner': [foreign_1],
                         'peer': [owner_token]},
            'reconverged': True,
            'writes': {'anchor': 41, 'landed': True},
            'final_probe': fenced(peer_token),
            'durable': {'owner': ['field_claim_lost',
                                  'field_claim_observed',
                                  'field_orphaned',
                                  'role_changed'],
                        'peer': ['field_claim_lost',
                                 'field_claim_observed',
                                 'field_orphaned',
                                 'role_changed']},
            'restored': True, 'restored_owner': owner_token}

    def expect(name, mutate, diagnostic=DIAG_FAILED):
        record = clean_record()
        mutate(record)
        found = {}
        _judge_holderless(
            record,
            lambda key, diag, detail: found.setdefault(key, diag))
        if diagnostic not in found.values():
            slipped.append(name)

    def wedge(record):
        record['resolution']['rows'] = [
            {'peer_role': 'standby', 'peer_sync': 'orphaned',
             'peer_answered': True, 'owner_role': 'standby',
             'owner_sync': 'orphaned', 'owner_answered': True,
             'probe': 'fenced', 'probe_owner': owner_token}] * 3

    # The doctored negative the issue names: the pair asserted
    # recovered while probe_writer still reports the holderless
    # claim standing and both peers stay orphaned.
    expect('asserted-recovered-orphaned', lambda record: (
        wedge(record),
        record.update({'final_probe': fenced(owner_token)})))
    # The wedge — neither bound reclaim takes the holderless claim —
    # lands the winnerless resolution.
    expect('wedged', lambda record: (
        wedge(record),
        record['resolution'].update({'winner': None}),
        record.update({'walk': []})))
    expect('wedged-served', lambda record: (
        wedge(record),
        record['resolution'].update({'winner': 'standby',
                                     'winner_side': 'peer',
                                     'winner_token': peer_token}),
        record.update({'walk': [], 'loser_moves': [],
                       'reconverged': False,
                       'writes': {'anchor': 41, 'landed': False},
                       'final_probe': fenced(owner_token)})))
    # The recovery's journaled origins.
    expect('operator-recovered', lambda record: record.update(
        {'walk': [('standby', 'promoting', 'request'),
                  ('promoting', 'active', 'request')]}))
    expect('failover-recovered', lambda record: record.update(
        {'walk': [('standby', 'promoting', 'failover'),
                  ('promoting', 'active', 'failover')]}))
    expect('unattributed-walk', lambda record: record.update(
        {'walk': [('standby', 'promoting', None),
                  ('promoting', 'active', None)]}), DIAG_NONDET)
    expect('walk-incomplete', lambda record: record.update(
        {'walk': [('standby', 'promoting', 'reclaim')]}))
    expect('loser-moved', lambda record: record['loser_moves']
           .append(('standby', 'promoting', 'reclaim')))
    expect('dual-active', lambda record:
           record['resolution']['rows'][-1].update(
               {'owner_role': 'active'}))
    # The held-window breaches — a live claim preempted, the field
    # opened, a peer promoted, a monitor dropped.
    expect('held-preempted', lambda record:
           record['hold'][1].update({'probe_owner': peer_token}))
    expect('held-open', lambda record:
           record['hold'][1].update({'probe': 'unclaimed',
                                     'probe_owner': None}))
    expect('held-moved', lambda record:
           record['hold'][1].update({'peer_role': 'promoting'}))
    expect('held-synchronized', lambda record:
           record['hold'][1].update({'owner_sync': 'tracking'}))
    expect('monitor-dropped', lambda record:
           record['hold'][1].update({'peer_role': None}))
    # The staged shapes that never formed.
    expect('demotion-held', lambda record: record.update(
        {'demoted': {'walked': False, 'walk': ['active'],
                     'unanswered': 0}}))
    expect('rejoin-wedged', lambda record: record.update(
        {'rejoined': {'tracked': False, 'unanswered': 0}}))
    expect('seized-foreign', lambda record: record.update(
        {'seized': fenced(peer_token)}))
    expect('peer-never-demoted', lambda record: record.update(
        {'peer_demoted': {'walked': False,
                          'walk': ['active'],
                          'unanswered': 0}}))
    expect('island-absent', lambda record:
           record.update({'island': None}))
    # The loss records.
    expect('loss-silent', lambda record:
           record.update({'marked_loss': []}))
    expect('loss-duplicated', lambda record: record.update(
        {'marked_loss': [{'point': 100, 'claimant': foreign_1}] * 2}))
    expect('loss-unattributed', lambda record:
           record.update({'marked_loss': [{'point': 100}]}))
    expect('loss-misattributed', lambda record:
           record.update({'marked_loss': [{'point': 100,
                                           'claimant': 0xDEAD}]}))
    expect('owner-loss-silent', lambda record:
           record.update({'owner_loss': []}))
    # The free and the placeholder.
    expect('freed-held', lambda record: record.update(
        {'freed': fenced(foreign_2)}))
    expect('placeholder-dropped', lambda record:
           record['placeholder'].update(
               {'verdict': {'result': 'error',
                            'error': {'kind': 'unclaimed',
                                      'detail': 'open'}},
                'named': None, 'standing': False}))
    expect('placeholder-raced', lambda record:
           record.update({'staged': {'result': 'error',
                                     'error': {'kind': 'fenced'}}}),
           DIAG_NONDET)
    # The recovery's proofs.
    expect('reconverge-wedged', lambda record:
           record.update({'reconverged': False}))
    expect('writes-stalled', lambda record:
           record['writes'].update({'landed': False}))
    expect('reseat-foreign', lambda record: record.update(
        {'final_probe': fenced(foreign_1)}))
    expect('reseat-open', lambda record: record.update(
        {'final_probe': {'result': 'error',
                         'error': {'kind': 'unclaimed'}}}))
    # The journal evidence.
    expect('observed-silent', lambda record:
           record['observed'].update({'owner': []}))
    expect('observed-unclaimed', lambda record:
           record['observed'].update({'owner': [], 'peer': []}))
    # The fenced peer's own loss record already attributed the held
    # claimant — a second record repeats one episode.
    expect('observed-duplicated', lambda record:
           record['observed'].update({'peer': [owner_token, foreign_1]}))
    expect('durable-absent', lambda record:
           record['durable'].update(
               {'peer': ['field_claim_lost', 'role_changed']}))
    # The restore.
    expect('unrestored', lambda record:
           record.update({'restored': False}))
    expect('restored-foreign', lambda record:
           record.update({'restored_owner': peer_token}))
    # The instability the contract does not answer for must report
    # nondeterministic, not failed.
    expect('promote-refused', lambda record: record.update(
        {'promote': {'status': 409, 'body': 'not_converged'}}),
        DIAG_NONDET)
    expect('claim-shared', lambda record:
           record['claim1'].update({'result': 'claimed_shared'}),
           DIAG_NONDET)
    expect('journal-read-dropped', lambda record:
           record.update({'marked_loss': None}), DIAG_NONDET)
    expect('probe-read-dropped', lambda record:
           record.update({'seized': None}), DIAG_NONDET)
    expect('hold-empty', lambda record:
           record.update({'hold': []}), DIAG_NONDET)
    expect('walk-read-dropped', lambda record:
           record.update({'walk': None}), DIAG_NONDET)
    return slipped


def scenario_holderless_claim_recovery(ctx):
    """With the deployed pair settled and tracking, POST /promote the
    standby so the active demotes and tracks, drive a foreign tool
    claim through the lane's claim-aware seam so the new owner is
    fenced and demotes orphaned, then free the field through a
    second attachment's claim/release while a staged unbound probe
    raises the cleared ex-owner's holderless claim — assert through
    both serving monitors, the claim surface, and the durable
    journals that the marked peer's bound reclaim re-seats the field
    unattended (never an operator promote), the recovered owner's
    writes land, and the pair reconverges; restore the launch
    roles."""
    case = Case(
        'holderless-claim-recovery',
        'Two successive orphaned ex-owners recover field ownership '
        'unattended once the foreign claim frees — the holderless '
        'standing claim never wedging the bound reclaim',
        'with the deployed pair settled and tracking, the '
        'standby\u2019s POST /promote supersedes the active (the '
        'first ex-owner, re-joining tracking with its loss mark '
        'cleared); a scenario attachment\u2019s claim_writer under a '
        'foreign tool token fences the promoted owner into the '
        'second orphaned ex-owner; a second attachment\u2019s '
        'claim_writer plus release_writer frees the field while a '
        'staged unbound ensure_writer raises the cleared ex-owner\'s '
        'holderless claim — the wedge shape; both peers\u2019 '
        'serving monitors, the probe_writer claim surface, and the '
        'durable --journal-file mirrors prove the designated bound '
        'reclaim re-seats the field unattended (origin=reclaim, no '
        'operator promote, no restart), the recovered owner\u2019s writes '
        'land, the loser reconverges to tracking, and the launch '
        'roles restore; two consecutive passes produce identical '
        'digests; failures report holderless-reclaim-failed / '
        'holderless-reclaim-nondeterministic, and the unchecked-'
        'diagnostic self-check covers the leg')
    try:
        active, standby = ctx.get('active'), ctx.get('standby')
        if not active or not standby:
            return case.finish(
                'inconclusive', 'the run config did not record both '
                'peer monitor endpoints — the leg cannot drive the '
                'deployed pair')
        placement = ctx.get('endpoint_placement') or {}
        if any(placement.get(name) != 'loopback'
               for name in ('active', 'standby', 'plant')):
            return case.finish(
                'inconclusive', 'the run config does not publish '
                'loopback endpoints for both peers and the plant — '
                'the declared placements are ' + str(placement)[:200])
        if not ctx.get('plant'):
            return case.finish(
                'inconclusive', 'the run config did not publish the '
                'simulated plant endpoint — the leg cannot read the '
                'field\u2019s claim surface')
        if ctx.get('plant_ctl') is None:
            return case.finish(
                'inconclusive', 'the run config carries no plant_ctl '
                'seam — the field census and write-landing reads '
                'cannot run')
        journal_files = ctx.get('journal_files') or {}
        if not all(journal_files.get(name)
                   for name in ('active', 'standby')):
            return case.finish(
                'inconclusive', 'the run config does not bind-mount '
                'per-controller journal files — the leg cannot '
                'audit the durable records')
        tokens = ctx.get('plant_owner') or {}
        if not all(tokens.get(name)
                   for name in ('active', 'standby')):
            return case.finish(
                'inconclusive', 'the run config records no pinned '
                '--owner-token for the pair — the standing claim\'s '
                'owner is not attributable')
        budget = ctx.get('failover_misses')
        if not budget:
            return case.finish(
                'inconclusive', 'the run config records no '
                '--auto-promote miss budget — the reclaim\'s '
                'recovery bound is unanchored')
        # The designated reclaim grants on the ex-owner's next scan,
        # so the resolution watch's bound must stay under the orphan
        # budget's own rescue window — the lane's 100 ms scan cadence
        # over the armed misses. A rig whose budget fires inside the
        # watch would let the budget's recorded holderless preemption
        # unwedge the placeholder in place of the reclaim the leg
        # exists to evidence, and the leg cannot tell the two apart.
        if HOLDERLESS_RESOLVE >= budget * 0.1:
            return case.finish(
                'inconclusive', 'the armed --auto-promote budget '
                'fires inside the ' + str(HOLDERLESS_RESOLVE) + 's '
                'resolution watch (' + str(budget) + ' misses) — the '
                'orphan budget\'s own rescue would mask the bound '
                'reclaim this leg judges')
        case.observe('subject pair — active ' + active
                     + ', standby ' + standby)

        reports = {name: _try_role(ctx, ctx[name])
                   for name in ('active', 'standby')}
        if all(report is None for report in reports.values()):
            return case.finish(
                'inconclusive', 'the pair is unreachable — monitor '
                'endpoints ' + active + ' and ' + standby)
        # The field's claim surface first, the sibling legs' order: a
        # field standing open, an unreadable probe, or a verdict
        # naming no owner is the rig's own shape, and it must be read
        # before the pair's posture is judged — a field with no
        # standing claim answers every later write unfenced, so the
        # baseline scans demote the launch owner into an orphan island
        # this leg would then read as a recovery failure it never
        # staged.
        baseline = _claim_probe(ctx)
        if baseline is None:
            return case.finish(
                'inconclusive', 'the plant\u2019s claim surface '
                'answered no probe — the leg cannot read the '
                'standing claim')
        if not _fenced(baseline):
            return case.finish(
                'inconclusive', 'the field held no standing writer '
                'claim at pass start — the claim surface is absent '
                'or the field is open: ' + str(baseline)[:200])
        if _probe_owner(baseline) is None:
            return case.finish(
                'inconclusive', 'the fencing verdict names no '
                'standing owner — the rig predates the '
                'loss-attribution contract: '
                + json.dumps(baseline)[:300])
        if _probe_monitor(baseline) is None:
            return case.finish(
                'inconclusive', 'the claim surface names no '
                'declared monitor — the staged run predates the '
                'field-arbitrated monitor contract')
        owner = _pair_active(ctx)
        if owner is None:
            return case.finish(
                'failed', 'holderless-reclaim-failed: no settled '
                'active peer — /role reports '
                + str(reports)[:300])
        if owner != 'active':
            _holderless_restore(ctx, 'active', 'standby')
            owner = wait_for(
                lambda: _pair_active(ctx) == 'active' and 'active'
                or None,
                time.monotonic() + HOLDERLESS_SETTLE,
                interval=HOLDERLESS_POLL)
        if owner != 'active':
            return case.finish(
                'inconclusive', 'the pair never settled on its '
                'launch layout — the unconfigured peer must hold '
                'the field for the episode the leg stages')
        if _probe_owner(baseline) != tokens[owner]:
            return case.finish(
                'inconclusive', 'the standing claim names '
                + str(_probe_owner(baseline)) + ' — the launch '
                'owner\u2019s token ' + str(tokens[owner])
                + ' was expected')
        peer = 'standby'
        if wait_for(lambda: _tracking_standby(ctx, peer) or None,
                    time.monotonic() + HOLDERLESS_SETTLE,
                    interval=HOLDERLESS_POLL) is None:
            return case.finish(
                'inconclusive', 'the pair never reported a settled '
                'tracking standby — the episode has no settled '
                'baseline')
        try:
            doc = _try_checkpoint(ctx, owner)
            if 'source_owns_field' not in doc \
                    or 'line_owner' not in doc:
                raise ValueError('missing fields')
        except Exception:
            return case.finish(
                'inconclusive', 'the pair serves a checkpoint '
                'document without the field-ownership stamps — the '
                'rig predates the field-arbitrated contract')
        case.observe('settled baseline: ' + owner + ' owns the '
                     'field under ' + str(tokens[owner])
                     + ' and ' + peer + ' tracks it')

        field_out = _field_out_points(ctx)
        if not field_out:
            return case.finish(
                'inconclusive', 'the simulated plant serves no '
                'field output to watch the owner\u2019s writes on')
        field_point = min(field_out)

        digests = []
        for number in (1, 2):
            violations = {}

            def note(key, diagnostic, detail):
                violations.setdefault(key, (diagnostic, detail))

            record, evidence = _holderless_pass(
                ctx, number, owner, peer, tokens, field_point)
            _judge_holderless(record, note)
            digest = _holderless_digest(record, violations)
            evidence['record'] = record
            evidence['digest'] = dict(digest)
            evidence['violations'] = {
                key: diagnostic for key, (diagnostic, _)
                in violations.items()}
            ref = save_evidence(
                ctx['evidence_dir'],
                'holderless-claim-recovery-pass-' + str(number)
                + '.json', evidence)
            case.evidence('file', ref, 'holderless-claim-recovery '
                          'pass ' + str(number) + ' — the promote '
                          'staging, the fenced demotion, the orphan '
                          'island, the held window, the staged '
                          'holderless placeholder, the resolution '
                          'watch, the journal audits, the restore, '
                          'and the normalized digest')
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
        case.observe('two holderless-claim recovery passes, '
                     'identical digests: '
                     + json.dumps(digests[0], sort_keys=True))

        # The unchecked-diagnostic self-check: the judge replays
        # each planted negative it must name; a silent audit means
        # the leg can no longer catch what it names.
        slipped = _holderless_self_check()
        if slipped:
            return case.finish('failed', DIAG_UNCHECKED
                               + ': planted negatives slipped the '
                               'leg\u2019s own audits: '
                               + ', '.join(slipped))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive',
                           'the leg could not complete on this '
                           'rig: ' + str(exc)[:500])
