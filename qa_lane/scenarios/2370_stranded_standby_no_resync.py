"""The stranded_standby_no_resync acceptance leg — one module per leg
of the scenario schedule; see qa_lane/scenarios/__init__.py for the
ordering rule and the shared seam."""
from .common import *

# Ordering: the leg restores what it moves — both takeover directions
# and the monitor-less window's re-seat land back on the launch role
# layout — so it needs no declared window.


# --------------------------------------------------------------------
# The claim-declared-monitor re-join contract (WW-LCM-001's continuity
# clause — the per-revision lane evidence for the contract decision
# 101 records and #1042/#1045 implement) answering QA finding
# `stranded-standby-no-resync`: a standby promoted while the field
# owner still runs preempts the live claim unconditionally; the
# superseded owner's first fenced write demotes it in place — and the
# demoted peer must NOT park `unsynchronized` forever. The verdict
# that fenced it carries the successor's declared monitor — the
# field-arbitrated candidate the decision names — and the demoted
# peer's tracking path resolves it: the keyed announced-verify on a
# keyed rig, the claim's own declaration where no peer hint can prove
# itself. The peer journals FieldClaimLost and TrackingSourceAdopted
# and reports `tracking` inside the lane's tick bound. The #1045
# amendment's window: a held tool claim declaring no monitor leaves
# the demoted peer un-converged only while no controller-owned claim
# re-seats the field with its own declared monitor.
#
# The leg runs the misordered promote in both directions — POST
# /promote on the tracking standby with no POST /demote on the owner
# first — then the monitor-less foreign-claim window where staging
# admits, and restores the launch layout. The claim op stays on the
# raw client — `claim_writer` is an op `dcs-plant-ctl` does not
# expose. Named diagnostics are stranded-standby-no-resync-failed and
# stranded-standby-no-resync-nondeterministic; two consecutive passes
# produce identical digests.

STRANDED_SETTLE = 30        # bound on each settle/rejoin watch
STRANDED_POLL = 0.4         # cadence polling the peers mid-episode
STRANDED_ROUNDS = 4         # polls the monitor-less hold window spans
STRANDED_DEADLINE = 15      # bound on the demotion/convergence waits
STRANDED_REJOIN_TICKS = 60  # the lane tick bound on the demoted peer's
                            # re-join — the finding's indefinite wedge
                            # is the failure this bounds
STRANDED_FOREIGN = 0x7161_2d73_7472_616e   # "qa-stran"
PAIR_PORTS = {'active': 8080, 'standby': 8081}
DIAG_FAILED = 'stranded-standby-no-resync-failed'
DIAG_NONDET = 'stranded-standby-no-resync-nondeterministic'


def _verdict_monitor(response):
    """The declared monitor the fencing verdict names (None absent)."""
    return ((response or {}).get('error') or {}).get('monitor')


def _verdict_owner(response):
    return ((response or {}).get('error') or {}).get('owner')


def _served_journal(ctx, name, floor):
    """The peer's served journal entries since `floor`, or None."""
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


def _durable_kinds(ctx, name):
    """The event kinds the peer's bind-mounted --journal-file holds."""
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


def _durable_adoptions(ctx, name):
    """The sources the peer's --journal-file tracking_source_adopted
    records name, or None when the file can't be read."""
    try:
        records = _journal_entries(ctx['journal_files'][name])
    except Exception:
        return None
    return [(record.get('entry') or {}).get('event', {})
            .get('tracking_source_adopted', {}).get('source')
            for record in records
            if 'tracking_source_adopted'
            in ((record.get('entry') or {}).get('event') or {})]


def _checkpoint(ctx, name):
    _, body = http_json('GET', ctx[name] + '/checkpoint')
    return body


def _roles(ctx, owner, peer):
    """The pair's /role reports, in one snapshot."""
    return {owner: _try_role(ctx, ctx[owner]),
            peer: _try_role(ctx, ctx[peer])}


def _sync_kind(report):
    """The sync vocabulary a standby /role report carries."""
    sync = ((report or {}).get('sync') or {})
    for kind in ('tracking', 'orphaned', 'diverged', 'unsynchronized'):
        if kind in sync:
            return kind
    return 'missing'


def _is_tracking(report):
    return _sync_kind(report) == 'tracking'


def _role_tick(report):
    tick = (report or {}).get('tick')
    return tick if isinstance(tick, int) else None


def _wait_standby(ctx, name, watch):
    """Poll /role until the peer reports standby; the poll rows land
    in `watch` for the timeline evidence."""
    def found():
        report = _try_role(ctx, ctx[name])
        watch.append({name: report})
        return report if (report or {}).get('role') == 'standby' \
            else None
    return wait_for(found, time.monotonic() + STRANDED_DEADLINE,
                    interval=STRANDED_POLL)


def _wait_tracking(ctx, name, watch):
    """Poll /role until the peer reports the tracking sync."""
    def found():
        report = _try_role(ctx, ctx[name])
        watch.append({name: report})
        if (report or {}).get('role') == 'standby' \
                and _is_tracking(report):
            return report
        return None
    return wait_for(found, time.monotonic() + STRANDED_SETTLE,
                    interval=STRANDED_POLL)


def _fencing_probe(ctx):
    """One raw `step` probe on the published plant port — the field's
    own fencing answer carrying owner and declared monitor."""
    return _try_plant(ctx, {'op': 'step', 'dt': 0})


def _rejoin_cycle(ctx, demoted, promoted, tokens, adoption_owed,
                  failed):
    """One involuntary-demote cycle: POST /promote on the tracking
    standby `promoted` while `demoted` still owns the field — never
    POST /demote on the owner first. Asserts the recorded contract
    on the demoted peer: demote-in-place, the attributed
    FieldClaimLost, the verdict naming the successor's declared
    monitor, `tracking` inside the lane's tick bound, the adopted
    line document, and the settled pair shape. `adoption_owed` is
    set for the launch owner — the peer the stranded-standby
    contract names, whose only tracking candidates the claim and
    the announced hints supply; the configured-source sibling
    re-joins without a journaled adoption and the event is only
    checked for consistency where it appears. Returns
    (digest, evidence)."""
    digest = {}
    evidence = {'demoted': demoted, 'promoted': promoted}
    floors = {name: _journal_cursor(ctx, ctx[name])
              for name in (demoted, promoted)}
    evidence['floors'] = floors
    port = str(PAIR_PORTS[promoted])

    status, body = _settle_call(ctx[promoted] + '/promote')
    evidence['promote'] = {'status': status, 'body': body}
    if status != 200:
        digest['promote'] = 'refused'
        failed('promote-' + promoted, 'POST /promote on the '
               'tracking standby answered ' + str(status) + ' '
               + json.dumps(body)[:300])
        return digest, evidence
    digest['promote'] = 'granted'

    # The demote-in-place watch: the superseded owner must walk to
    # standby on its own fenced write — never disappear, never hold
    # 'active' alongside the promoted peer.
    demotion = []
    walked = _wait_standby(ctx, demoted, demotion)
    evidence['demotion'] = demotion[-8:]
    if walked is None:
        digest['demotion'] = 'held'
        last = demotion[-1].get(demoted) if demotion else None
        failed('demotion-' + demoted, 'the superseded peer never '
               'reported standby — last /role ' + str(last)[:300])
        return digest, evidence
    walk = [row.get(demoted, {}).get('role') for row in demotion]
    if any(role not in ('active', 'promoting', 'demoting', 'standby')
           for role in walk):
        failed('walk', 'the demoted peer reported an unexpected '
               'role walk ' + str(walk)[:200])
    digest['demotion'] = 'in-place'
    demote_tick = _role_tick(walked)

    # The journaled loss the demotion owes — attributed to the
    # promoted peer's pinned owner token.
    losses = _journaled(ctx, demoted, floors[demoted],
                        'field_claim_lost') or []
    evidence['field_claim_lost'] = losses
    if not losses:
        digest['loss'] = 'silent'
        failed('loss-' + demoted, 'the demoted peer journaled no '
               'field_claim_lost during the supersede')
    else:
        claimants = [body.get('claimant') for body in losses]
        if len(losses) > 1:
            digest['loss'] = 'duplicated'
            failed('loss-' + demoted, 'the demoted peer journaled '
                   + str(len(losses)) + ' field_claim_lost records '
                   'for one supersede')
        elif not all('claimant' in body for body in losses):
            digest['loss'] = 'unattributed'
            failed('loss-' + demoted, 'the journaled field_claim_lost '
                   'records carry no claimant field: '
                   + str(losses)[:300])
        elif claimants != [tokens[promoted]] * len(losses):
            digest['loss'] = 'misattributed'
            failed('loss-' + demoted, 'the journaled '
                   'field_claim_lost names ' + str(claimants)
                   + ' — the promoted peer\u2019s owner token '
                   + str(tokens[promoted]) + ' was expected')
        else:
            digest['loss'] = 'attributed'

    # The claim's declared monitor — the field-arbitrated candidate
    # decision 101 names — answered on a fresh fencing probe.
    probe = _fencing_probe(ctx)
    evidence['verdict'] = probe
    declared = _verdict_monitor(probe)
    evidence['declared'] = declared
    if _verdict_owner(probe) != tokens[promoted]:
        digest['declared'] = 'foreign'
        failed('declared-' + promoted, 'the promoted peer never '
               'stood as claim owner: ' + str(probe)[:300])
    elif declared is None:
        digest['declared'] = 'absent'
        failed('declared-' + promoted, 'the promoted peer\u2019s '
               'claim declares no monitor — the contract this leg '
               'proves')
    elif not str(declared).endswith(':' + port):
        digest['declared'] = 'misnamed'
        failed('declared-' + promoted, 'the declared monitor '
               + str(declared) + ' does not resolve the promoted '
               'peer on :' + port)
    else:
        digest['declared'] = 'named'

    # The re-join: the demoted peer resolves the standing claim's
    # declared monitor through the verified tracking path and
    # reports tracking inside the lane's tick bound.
    rejoin = []
    tracked = _wait_tracking(ctx, demoted, rejoin)
    evidence['rejoin'] = rejoin[-8:]
    if tracked is None:
        digest['rejoin'] = 'wedged'
        last = rejoin[-1].get(demoted) if rejoin else None
        failed('rejoin-' + demoted, 'the demoted peer never '
               're-joined — the stranded-standby wedge the finding '
               'names; last /role ' + str(last)[:300])
        return digest, evidence
    tick_delta = None
    track_tick = _role_tick(tracked)
    if demote_tick is not None and track_tick is not None:
        tick_delta = track_tick - demote_tick
    evidence['rejoin_ticks'] = tick_delta
    if tick_delta is not None and tick_delta > STRANDED_REJOIN_TICKS:
        digest['rejoin'] = 'bounded-miss'
        failed('rejoin-' + demoted, 'the demoted peer took '
               + str(tick_delta) + ' ticks to re-join — beyond the '
               'lane bound ' + str(STRANDED_REJOIN_TICKS))
    else:
        digest['rejoin'] = 'tracked'

    # The verified-source evidence: the demoted peer resolves the
    # endpoint the standing claim declared. A fresh demotion journals
    # its TrackingSourceAdopted; a repeat demotion may converge
    # through the process-lifetime pin an earlier adoption left —
    # either way the durable journal must carry an adoption naming
    # the declared monitor's port. The configured-source sibling owes
    # none — its declared --standby source needs no verification.
    adoptions = _journaled(ctx, demoted, floors[demoted],
                           'tracking_source_adopted') or []
    evidence['tracking_source_adopted'] = adoptions
    durable_sources = _durable_adoptions(ctx, demoted)
    evidence['durable_sources'] = durable_sources
    sources = [(body or {}).get('source') for body in adoptions]
    pinned = any(str(source).endswith(':' + port)
                 for source in (durable_sources or []))
    if len(adoptions) > 1:
        digest['adopted'] = 'duplicated'
        failed('adopted-' + demoted, 'the demoted peer journaled '
               + str(len(adoptions)) + ' tracking_source_adopted '
               'records for one re-join: ' + str(sources)[:200])
    elif len(adoptions) == 1:
        if str(sources[0]).endswith(':' + port):
            digest['adopted'] = 'resolved'
        else:
            digest['adopted'] = 'foreign'
            failed('adopted-' + demoted, 'the adopted source '
                   + str(sources[0]) + ' does not resolve the '
                   'endpoint the standing claim declared (:'
                   + port + ')')
    elif adoption_owed:
        if pinned:
            digest['adopted'] = 'resolved'
        else:
            digest['adopted'] = 'silent'
            failed('adopted-' + demoted, 'the demoted peer resolved '
                   'no verified tracking source — neither a journaled '
                   'adoption this demotion nor a durable pin naming '
                   'the declared monitor on :' + port)
    else:
        digest['adopted'] = 'configured'

    # The adopted line document the demoted peer now serves — this
    # line's document checks plus the keyed line_proof the rig's
    # pair token arms on the verified pull.
    try:
        doc = _checkpoint(ctx, demoted)
        evidence['checkpoint'] = doc
        if doc.get('source_owns_field') is not True:
            digest['document'] = 'foreign'
            failed('document-' + demoted, 'the re-joined peer serves '
                   'a checkpoint whose source_owns_field is '
                   + str(doc.get('source_owns_field')))
        elif not str(doc.get('line_owner')).endswith(':' + port):
            digest['document'] = 'misnamed'
            failed('document-' + demoted, 'the re-joined peer serves '
                   'line_owner ' + str(doc.get('line_owner'))
                   + ' — expected the promoted peer on :' + port)
        else:
            digest['document'] = 'verified'
    except Exception as exc:
        evidence['checkpoint'] = str(exc)
        digest['document'] = 'unreadable'
        failed('document-' + demoted, 'the re-joined peer serves no '
               'checkpoint document: ' + str(exc)[:200])

    # The durable mirror: the bind-mounted --journal-file carries
    # the same loss and adoption records.
    durable = _durable_kinds(ctx, demoted)
    evidence['durable'] = sorted(durable) if durable else None
    if durable is not None:
        if 'field_claim_lost' not in durable:
            failed('durable-' + demoted, 'the durable journal file '
                   'carries no field_claim_lost record')
        if adoption_owed \
                and 'tracking_source_adopted' not in durable:
            failed('durable-' + demoted, 'the durable journal file '
                   'carries no tracking_source_adopted record')

    # The settled pair shape: exactly one active — the promoted
    # peer — with the demoted owner tracking it.
    roles = _roles(ctx, demoted, promoted)
    evidence['roles'] = roles
    actives = [name for name, report in roles.items()
               if (report or {}).get('role') == 'active']
    if actives != [promoted]:
        digest['pair'] = 'split'
        failed('pair', 'the settled pair reports ' + str(actives)
               + ' active — expected [' + repr(promoted)
               + '] alone')
    elif not _is_tracking(roles[demoted]):
        digest['pair'] = 'untracked'
        failed('pair', 'the demoted peer reports '
               + str(roles[demoted])[:300])
    else:
        digest['pair'] = 'settled'
    return digest, evidence


def _monitor_less_window(ctx, owner, peer, tokens, failed):
    """The #1045 amendment's window: a held tool claim declaring no
    monitor leaves the demoted peer un-converged only while no
    controller-owned claim re-seats the field. Returns
    (digest, evidence)."""
    digest = {}
    evidence = {}
    floors = {name: _journal_cursor(ctx, ctx[name])
              for name in (owner, peer)}
    foreign = _plant_connect(ctx)
    try:
        claim = _plant_request(foreign, {'op': 'claim_writer',
                                         'owner': STRANDED_FOREIGN,
                                         'controller': False})
        evidence['claim'] = claim
        if claim.get('result') != 'done':
            raise ConnectionError(
                'the monitor-less foreign claim was refused — the '
                'claim-staging lever is unavailable: '
                + str(claim)[:300])
        digest['claim'] = 'held'

        verdict = _fencing_probe(ctx)
        evidence['verdict'] = verdict
        if _verdict_owner(verdict) != STRANDED_FOREIGN:
            digest['verdict'] = 'foreign'
            failed('window', 'the tool claim never stood as field '
                   'owner: ' + str(verdict)[:300])
        elif 'monitor' in ((verdict or {}).get('error') or {}):
            digest['verdict'] = 'named'
            failed('window', 'a tool claim that declared no monitor '
                   'names one in the fencing verdict: '
                   + str(verdict)[:300])
        else:
            digest['verdict'] = 'monitor-less'

        # The superseded owner demotes in place under the tool
        # claim — the demotion machinery owes the loss journal.
        demotion = []
        walked = _wait_standby(ctx, owner, demotion)
        evidence['demotion'] = demotion[-8:]
        if walked is None:
            digest['demotion'] = 'held'
            failed('window-demote', 'the superseded peer never '
                   'reported standby under the tool claim')
            return digest, evidence
        digest['demotion'] = 'in-place'
        losses = _journaled(ctx, owner, floors[owner],
                            'field_claim_lost') or []
        evidence['window_loss'] = losses
        if losses and all(body.get('claimant') == STRANDED_FOREIGN
                          for body in losses):
            digest['window_loss'] = 'attributed'
        else:
            digest['window_loss'] = 'unattributed'
            failed('window-loss', 'the tool-claim supersede '
                   'journaled no attributed field_claim_lost: '
                   + str(losses)[:300])

        # The hold window: while the monitor-less tool claim stands
        # the demoted peer must NOT converge — `unsynchronized`, or
        # the keyed orphaned-tracked reading — and the verdict keeps
        # naming the monitor-less owner.
        hold, parked = [], set()
        for _ in range(STRANDED_ROUNDS):
            hold.append(_roles(ctx, owner, peer))
            time.sleep(STRANDED_POLL)
        evidence['hold'] = hold
        for row in hold:
            parked.add(_sync_kind(row.get(owner)))
        evidence['parked'] = sorted(parked)
        if 'tracking' in parked:
            digest['parked'] = 'tracking'
            failed('window-park', 'the demoted peer reported '
                   'tracking while the field stood under a '
                   'monitor-less tool claim')
        elif 'missing' in parked:
            digest['parked'] = 'silent'
            failed('window-park', 'the demoted peer stopped '
                   'answering /role during the window')
        else:
            digest['parked'] = 'unconverged'
        if any((row.get(peer) or {}).get('role') == 'active'
               for row in hold):
            failed('window-peer', 'the sibling promoted itself '
                   'during the monitor-less window')

        # The re-seat: the sibling's promote lands a
        # controller-owned claim — a tool claim is never an
        # incumbent — and the demoted peer then sees a declared
        # monitor and converges.
        status, body = _settle_call(ctx[peer] + '/promote')
        evidence['reseat'] = {'status': status, 'body': body}
        if status != 200:
            digest['reseat'] = 'refused'
            failed('window-reseat', 'the sibling promote under the '
                   'tool claim answered ' + str(status) + ' '
                   + json.dumps(body)[:300])
            return digest, evidence
        digest['reseat'] = 'controller'
        verdict = _fencing_probe(ctx)
        evidence['reseat_verdict'] = verdict
        port = str(PAIR_PORTS[peer])
        if _verdict_owner(verdict) != tokens[peer]:
            digest['reseat_verdict'] = 'foreign'
            failed('window-reseat', 'the sibling\u2019s controller '
                   'claim never stood as owner after the preempt: '
                   + str(verdict)[:300])
        elif not str(_verdict_monitor(verdict)).endswith(':' + port):
            digest['reseat_verdict'] = 'absent'
            failed('window-reseat', 'the re-seated claim declares '
                   'no monitor for the re-join to resolve: '
                   + str(verdict)[:300])
        else:
            digest['reseat_verdict'] = 'named'

        converge = []
        tracked = _wait_tracking(ctx, owner, converge)
        evidence['converge'] = converge[-8:]
        if tracked is None:
            digest['converge'] = 'wedged'
            last = converge[-1].get(owner) if converge else None
            failed('window-converge', 'the demoted peer stayed '
                   'un-converged after a controller-owned claim '
                   're-seated the field — last /role '
                   + str(last)[:300])
        else:
            digest['converge'] = 'tracked'
    finally:
        foreign.close()
    return digest, evidence


def _restore_layout(ctx, owner, peer):
    """Best-effort launch-layout restore: demote whichever peer still
    owns the field, promote the launch owner, and let the pair
    reconverge."""
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
                 time.monotonic() + STRANDED_DEADLINE,
                 interval=STRANDED_POLL)
        wait_for(lambda: _tracking_standby(ctx, peer) or None,
                 time.monotonic() + STRANDED_SETTLE,
                 interval=STRANDED_POLL)
    except Exception:
        pass


def _stranded_pass(ctx, number, owner, tokens, keyed):
    """One full leg pass: both involuntary-demote directions, the
    monitor-less foreign-claim window, and the launch-layout
    restore. Returns (digest, violations, evidence)."""
    violations = {}
    peer = 'standby' if owner == 'active' else 'active'

    def note(key, detail):
        violations[key] = violations.get(key) or []
        violations[key].append(detail)

    def failed(key, detail):
        note(key + ' ' + DIAG_FAILED, detail)

    evidence = {'pass': number, 'owner': owner, 'keyed': keyed,
                'foreign_token': STRANDED_FOREIGN}
    digest = {}
    try:
        # Cycle 1 — the tracking standby promotes; the launch owner
        # demotes in place and re-joins through the declared monitor.
        cycle, evidence['cycle1'] = _rejoin_cycle(
            ctx, owner, peer, tokens, True, failed)
        digest['cycle1'] = cycle
        if cycle.get('promote') != 'granted':
            return digest, violations, evidence

        # Cycle 2 — the launch owner promotes back through the same
        # involuntary entry; the demoted sibling re-joins through
        # its configured --standby source (no adoption journal owed).
        cycle, evidence['cycle2'] = _rejoin_cycle(
            ctx, peer, owner, tokens, False, failed)
        digest['cycle2'] = cycle

        # The #1045 amendment's window — the published plant port
        # always admits it; a refused claim means the lever is
        # absent and the leg lands inconclusive.
        cycle, evidence['window'] = _monitor_less_window(
            ctx, owner, peer, tokens, failed)
        digest['window'] = cycle
    finally:
        _restore_layout(ctx, owner, peer)
        restored = (_try_role(ctx, ctx[owner]) or {}).get('role') \
            == 'active' and _tracking_standby(ctx, peer)
        digest['restore'] = 'restored' if restored else 'unrestored'
        if not restored:
            failed('restore', 'the launch layout never restored — '
                   'owner /role '
                   + str(_try_role(ctx, ctx[owner]))[:200]
                   + ' peer /role '
                   + str(_try_role(ctx, ctx[peer]))[:200])
    return digest, violations, evidence


def scenario_stranded_standby_no_resync(ctx):
    """A standby promoted while the field owner still runs preempts
    the live claim unconditionally; the superseded owner's first
    fenced write demotes it in place, and the recorded contract
    answers the stranded-standby wedge: the fencing verdict carries
    the successor's declared monitor — the field-arbitrated
    candidate decision 101 names — the demoted peer resolves it
    through the verified tracking path, journals FieldClaimLost and
    TrackingSourceAdopted, and reports `tracking` inside the lane's
    tick bound. Both promote directions prove the pair stays
    promotable either way; a held tool claim declaring no monitor
    leaves the demoted peer un-converged only until a
    controller-owned claim re-seats the field (#1045); the launch
    layout restores."""
    case = Case('stranded-standby-no-resync',
                'The involuntary promote preempts the claim; the '
                'ex-owner demotes in place, resolves the declared '
                'monitor, journals FieldClaimLost/'
                'TrackingSourceAdopted, and re-joins inside the '
                'bound.',
                'POST /promote on the tracking standby (no POST '
                '/demote first): the promoted peer claims and scans; '
                'the ex-owner demotes in place, journals the '
                'attributed FieldClaimLost, adopts the endpoint the '
                'standing claim declares, and re-joins tracking '
                'inside the bound; a monitor-less tool claim leaves '
                'the demoted peer un-converged only until a '
                'controller-owned claim re-seats the field; the '
                'launch layout restores.')
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
                'the rig\u2019s declared placements are '
                + str(placement)[:200] + ' so the leg cannot drive '
                'the pair or stage the foreign claim host-side')
        if not ctx.get('plant'):
            return case.finish(
                'inconclusive', 'the run config did not publish the '
                'simulated plant endpoint — the leg cannot stage '
                'the monitor-less foreign claim or read the '
                'field\u2019s fencing answers')
        if not ctx.get('plant_ctl'):
            return case.finish(
                'inconclusive', 'the run config did not record the '
                'plant-control exec path — the leg cannot read the '
                'field census')
        journal_files = ctx.get('journal_files') or {}
        if not all(journal_files.get(name)
                   for name in ('active', 'standby')):
            return case.finish(
                'inconclusive', 'the run config does not bind-mount '
                'per-controller journal files — the leg cannot '
                'audit the durable FieldClaimLost and '
                'TrackingSourceAdopted records')
        tokens = ctx.get('plant_owner') or {}
        if not all(tokens.get(name) for name in ('active', 'standby')):
            return case.finish(
                'inconclusive', 'the run config records no pinned '
                '--owner-token for the pair — the promoted '
                'peers\u2019 claims are not attributable')
        keyed = bool(ctx.get('pair_token'))
        case.observe('subject pair ' + ('keyed' if keyed
                                        else 'unkeyed')
                     + ' — active ' + active + ', standby ' + standby)
        owner = _pair_active(ctx)
        if owner is None:
            reports = _roles(ctx, 'active', 'standby')
            if any(report is not None for report in reports.values()):
                return case.finish(
                    'failed', 'no settled active peer — /role '
                    'reports ' + str(reports)[:300])
            return case.finish(
                'inconclusive', 'the pair is unreachable — monitor '
                'endpoints ' + active + ' and ' + standby)
        peer = 'standby' if owner == 'active' else 'active'
        settled = wait_for(lambda: _tracking_standby(ctx, peer)
                           or None,
                           time.monotonic() + STRANDED_SETTLE,
                           interval=STRANDED_POLL)
        if not settled:
            return case.finish(
                'inconclusive', 'the pair never reported a settled '
                'tracking standby — the re-join episode has no '
                'settled baseline')
        try:
            doc = _checkpoint(ctx, owner)
            if 'source_owns_field' not in doc \
                    or 'line_owner' not in doc:
                raise ValueError('missing fields')
        except Exception:
            return case.finish(
                'inconclusive', 'the pair serves a checkpoint '
                'document without the field-ownership stamps — the '
                'rig predates the field-arbitrated monitor '
                'contract')
        census = _try_plant_ctl(ctx, 'list')
        if census is None:
            return case.finish(
                'inconclusive', 'the shipped plant tool answered no '
                'field census — the plant_ctl seam is not serving')
        case.observe('field census '
                     + str(len(census.get('points') or []))
                     + ' points read through the shipped tool')
        baseline = _fencing_probe(ctx)
        if not _fenced(baseline) \
                or _verdict_owner(baseline) != tokens[owner]:
            return case.finish(
                'inconclusive', 'the baseline fencing probe did not '
                'name the settled owner — the field is open or the '
                'fencing surface is absent: ' + str(baseline)[:200])
        if _verdict_monitor(baseline) is None:
            return case.finish(
                'inconclusive', 'the fencing verdict names no '
                'declared monitor — the rig predates the '
                'field-arbitrated monitor contract')
        case.observe('settled baseline: ' + owner + ' owns the '
                     'field under ' + str(tokens[owner]) + ' and '
                     + peer + ' tracks it')

        digests = []
        for number in (1, 2):
            digest, violations, evidence = _stranded_pass(
                ctx, number, owner, tokens, keyed)
            ref = save_evidence(
                ctx['evidence_dir'], 'stranded-standby-no-resync-pass-'
                + str(number) + '.json', evidence)
            case.evidence('file', ref, 're-join episode pass '
                          + str(number))
            if violations:
                detail = '; '.join(key + ': ' + '; '.join(
                    str(v)[:400] for v in values)
                    for key, values in sorted(violations.items()))
                return case.finish('failed', detail)
            digests.append(digest)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', DIAG_NONDET + ' — consecutive passes '
                'diverged: '
                + json.dumps(digests, sort_keys=True)[:1800])
        case.observe('two consecutive re-join episodes produced '
                     'identical digests: '
                     + json.dumps(digests[0], sort_keys=True))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive',
                           'the leg could not complete on this '
                           'rig: ' + str(exc)[:500])
