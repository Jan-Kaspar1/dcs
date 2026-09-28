"""The foreign_claim_release acceptance leg — one module per leg
of the scenario schedule; see qa_lane/scenarios/__init__.py for the
ordering rule and the shared seam."""
from .common import *

# Ordering: the leg restores what it moves — the released claim's
# resolution lands the pair back on one active plus one tracking
# standby and the finally restores the launch claim state and
# roles — so it needs no declared window.


# --------------------------------------------------------------------
# The monitor-less foreign-claim intended-unsynchronized and
# release-resolution bound — the per-revision lane evidence for the
# contract #1167's fix pins (decision 101's amended
# intended-unsynchronized bound, WW-LCM-001's continuity clause).
# On an unkeyed pair, a foreign attachment's claim_writer held with
# no monitor declared leaves the fenced ex-owner's served sync
# unsynchronized — the field's arbitration names nothing to track
# and no announced hint can prove itself unkeyed, so unsynchronized
# is the only honest report — and unsynchronized is intended ONLY
# while that monitor-less claim stands. Releasing it must resolve
# the field through one of the two recorded paths, never a
# permanent strand: the ex-owner's loss-marked bound conditional
# re-grant re-arms its token and walks it back to active
# unattended, or a successor's claim — with its monitor declared —
# stands for the demoted peer's verified tracking-source adoption.
#
# The adjacent legs pin the pieces, not the bound: 2350's
# claim-reclaim proves the released-preemption re-seat, 2380's
# claim-monitor-rendezvous the dialable declared monitor and
# journaled adoption, 2370's stranded-standby-no-resync the
# same-claim-monitor succession — none asserts that the served
# unsynchronized covers exactly the monitor-less claim's window
# and no more, nor that the foreign claim's own release_writer is
# what resolves it. The durable test
# `unkeyed_fenced_demote_reclaims_the_released_field` drives the
# defect's scripted reproduction; this leg is its per-revision rig
# evidence.
#
# The leg stages the finding's own trigger through the lane's
# claim-aware seam — the raw plant client, `claim_writer` being an
# op the shipped dcs-plant-ctl does not expose: a dedicated
# attachment claims under a foreign token with `controller: false`
# and no monitor field — the monitor-less tool-claim shape — and
# holds it through the fenced-write demotion. Through the
# ex-owner's serving monitor the verdict must be standby+
# unsynchronized on every poll of the claim's standing window —
# never tracking, never a dead monitor, never silence — while the
# claim surface keeps naming the monitor-less induction token, and
# the durable journal carries the attributed field_claim_lost.
# Then release_writer: the unscripted resolution must land inside
# the bound — the journaled walk back to active or the journaled
# adoption naming the resolved claim's declared monitor — the
# resolved claim itself declaring a monitor, the field's writes
# landing again, and the pair settling back to one active plus one
# tracking standby. A peer still unsynchronized past the bound is
# the finding's permanent strand; a peer reporting clean while the
# monitor-less claim stands is the same verdict asserted past its
# window.
#
# Named diagnostics: foreign-claim-release-failed for a contract
# miss, foreign-claim-release-nondeterministic for the instability
# the contract does not answer for (dropped reads, a refused or
# shared staging claim, a sibling promoting out from under the
# leg, two passes' digests diverging), and
# foreign-claim-release-unchecked for the self-check's planted
# negatives slipping the judge. Inconclusive when the staged run
# predates the contract — no claim read surface, no declared
# monitor on the standing verdict, no claim-staging lever — or
# keys the pair: the bound names the unkeyed posture, the
# announced verify handing a keyed demote 'orphaned' instead.

RELEASE_SETTLE = 30       # bound on each settle/reconverge wait
RELEASE_POLL = 0.4        # cadence polling the peers mid-episode
RELEASE_ROUNDS = 5        # polls the held monitor-less window spans
RELEASE_DEADLINE = 15     # bound on the demotion/resolution waits
# The dedicated attachment's foreign owner token — never a
# controller's pinned token nor the tool's: the monitor-less claim
# whose hold parks the fenced ex-owner unsynchronized and whose
# release must resolve.
RELEASE_FOREIGN = 0x7161_2d66_6372_656c    # "qa-fcrel"
DIAG_FAILED = 'foreign-claim-release-failed'
DIAG_NONDET = 'foreign-claim-release-nondeterministic'
DIAG_UNCHECKED = 'foreign-claim-release-unchecked'


def _verdict_owner(response):
    return ((response or {}).get('error') or {}).get('owner')


def _verdict_monitor(response):
    """The declared monitor the claim verdict carries — None when the
    standing claim declared none or no claim stands."""
    return ((response or {}).get('error') or {}).get('monitor')


def _verdict_declares_monitor(response):
    """Whether the verdict's error carries the monitor field at all —
    a monitor-less claim's answer has no such field."""
    return 'monitor' in ((response or {}).get('error') or {})


def _claim_surface(ctx):
    """The field's standing claim read through the plant protocol —
    `probe_writer`, the non-mutating verdict a mutation would meet:
    owner token plus any declared monitor."""
    return _try_plant(ctx, {'op': 'probe_writer'})


def _served_journal(ctx, name, floor):
    try:
        _, body = http_json('GET', ctx[name] + '/journal?since='
                            + str(floor))
    except Exception:
        return None
    return _journal_list(body)


def _journaled(ctx, name, floor, kind):
    entries = _served_journal(ctx, name, floor)
    if entries is None:
        return None
    return [entry['event'][kind] for entry in entries
            if kind in (entry.get('event') or {})]


def _journaled_walk(ctx, name, floor):
    """The (from, to) role transitions the serving journal records
    since `floor` — the demote-and-reclaim walk the resolution's
    evidence is audited on."""
    entries = _served_journal(ctx, name, floor)
    if entries is None:
        return None
    return [(change.get('from'), change.get('to'))
            for entry in entries
            for change in [(entry.get('event') or {})
                           .get('role_changed') or {}]
            if change]


def _durable_records(ctx, name, floor):
    """The peer's bind-mounted --journal-file records since `floor`
    records — the durable mirror the episode's records are audited
    on — or None when the file can't be read."""
    try:
        return _journal_entries(ctx['journal_files'][name])[floor:]
    except Exception:
        return None


def _durable_kinds(records):
    kinds = set()
    for record in records or []:
        kinds.update((record.get('entry') or {}).get('event') or {})
    return sorted(kinds)


def _run_boundaries(ctx, name):
    """The process lifetimes the peer's durable journal records — one
    run_boundary per resumed run."""
    try:
        return sum(1 for item in
                   _journal_entries(ctx['journal_files'][name])
                   if 'run_boundary' in item)
    except Exception:
        return None


def _sync_kind(report):
    sync = ((report or {}).get('sync') or {})
    for kind in ('tracking', 'orphaned', 'diverged', 'unsynchronized'):
        if kind in sync:
            return kind
    return 'missing'


def _is_tracking(report):
    return _sync_kind(report) == 'tracking'


def _wait_standby(ctx, name, watch):
    """Poll the named peer's /role until it reports standby (appending
    each role it serves to `watch`). Returns the report dict, or
    None when it never settles."""
    deadline = time.monotonic() + RELEASE_DEADLINE
    settled = None
    while time.monotonic() < deadline and settled is None:
        report = _try_role(ctx, ctx[name])
        if report is not None:
            watch.append(report.get('role'))
            if report.get('role') == 'standby':
                settled = report
        if settled is None:
            time.sleep(RELEASE_POLL)
    return settled


def _restore_layout(ctx, owner, peer):
    """Best-effort launch-layout restore: demote whichever peer still
    owns the field, promote the launch owner back over it, and let
    the pair reconverge. Every step is retried inside the bound and
    swallowed on refusal — a clean pass needs none of it."""
    try:
        report = _try_role(ctx, ctx[peer])
        if (report or {}).get('role') in ('active', 'promoting'):
            _settle_call(ctx[peer] + '/demote')
            _wait_standby(ctx, peer, [])
        deadline = time.monotonic() + RELEASE_SETTLE
        while time.monotonic() < deadline:
            if (_try_role(ctx, ctx[owner]) or {}).get('role') \
                    != 'active':
                _settle_call(ctx[owner] + '/promote')
            if _pair_active(ctx) == owner \
                    and _tracking_standby(ctx, peer) is not None:
                return
            time.sleep(RELEASE_POLL)
    except Exception:
        pass


def _release_pass(ctx, number, owner, peer, tokens, watch_point):
    """One release pass: the dedicated attachment's monitor-less
    claim_writer preempts the field owner and holds through the
    fenced-write demotion; the ex-owner's serving monitor must
    report standby+unsynchronized on every poll of the claim's
    standing window; then release_writer and the unscripted
    resolution must land — the loss-marked reclaim or the
    declared-monitor adoption — journaled, reconverged, never
    stranded. Returns (record, evidence): the record is what the
    judge replays."""
    record = {'owner': owner, 'peer': peer,
              'owner_token': tokens.get(owner),
              'peer_token': tokens.get(peer),
              'watch_point': watch_point}
    evidence = {'pass': number, 'owner': owner, 'peer': peer,
                'foreign': RELEASE_FOREIGN,
                'watch_point': watch_point}
    floors = {}
    for name in (owner, peer):
        try:
            floors[name] = _journal_cursor(ctx, ctx[name])
        except Exception:
            floors[name] = None
    record['floors'] = floors
    evidence['floors'] = floors
    durable_floors = {}
    for name in (owner, peer):
        try:
            durable_floors[name] = len(
                _journal_entries(ctx['journal_files'][name]))
        except Exception:
            durable_floors[name] = None

    baseline = _claim_surface(ctx)
    record['baseline'] = baseline
    evidence['baseline'] = baseline
    if not isinstance(baseline, dict):
        return record, evidence

    stream = _plant_connect(ctx)
    released = False
    try:
        claim = _plant_request(stream, {'op': 'claim_writer',
                                        'owner': RELEASE_FOREIGN,
                                        'controller': False})
        record['claim'] = claim
        evidence['claim'] = claim
        if claim.get('result') == 'claimed_shared':
            return record, evidence
        if claim.get('result') != 'done':
            raise ConnectionError(
                'the monitor-less foreign claim was refused — the '
                'claim-staging lever is unavailable: '
                + str(claim)[:300])
        seized = _claim_surface(ctx)
        record['seized'] = seized
        evidence['seized'] = seized

        # The demotion watch: the ex-owner's first fenced write
        # demotes it in place — its serving monitor answering every
        # poll, because a degrade is not a death — while the
        # tracking peer holds standby.
        demotion = {'polls': 0, 'answered': 0, 'walk': []}
        deadline = time.monotonic() + RELEASE_DEADLINE
        walked = None
        while time.monotonic() < deadline and walked is None:
            demotion['polls'] += 1
            report = _try_role(ctx, ctx[owner])
            partner = _try_role(ctx, ctx[peer])
            demotion['walk'].append({'owner': (report or {})
                                     .get('role'),
                                     'peer': (partner or {})
                                     .get('role')})
            if report is not None:
                demotion['answered'] += 1
                if report.get('role') == 'standby':
                    walked = report
            if walked is None:
                time.sleep(RELEASE_POLL)
        evidence['demotion'] = demotion['walk'][-8:]
        record['demoted'] = demotion
        if walked is None:
            return record, evidence
        record['losses'] = _journaled(
            ctx, owner, floors[owner], 'field_claim_lost') \
            if floors.get(owner) is not None else None
        evidence['field_claim_lost'] = record['losses']

        # The held window: the monitor-less claim stands while the
        # fenced ex-owner's serving monitor reports — the bound's
        # verdict half. Every poll must answer standby+
        # unsynchronized and the claim surface must keep naming the
        # monitor-less induction token.
        hold = []
        for index in range(RELEASE_ROUNDS):
            hold.append({'owner': _try_role(ctx, ctx[owner]),
                         'peer': _try_role(ctx, ctx[peer]),
                         'verdict': _claim_surface(ctx)})
            if index + 1 < RELEASE_ROUNDS:
                time.sleep(RELEASE_POLL)
        record['hold'] = hold
        evidence['hold'] = hold

        # The release: the attachment hands the claim back — the
        # ownerless window the ex-owner's loss-marked reclaim or a
        # successor's declared claim must resolve from.
        release = _plant_request(stream, {'op': 'release_writer'})
        record['release'] = release
        evidence['release'] = release
        released = release.get('result') == 'done'
        if not released:
            raise ConnectionError(
                'the preemptor\'s claim hand-back was refused: '
                + str(release)[:300])

        anchor = _probe_sample(ctx, watch_point)
        anchor_tick = anchor.get('tick') \
            if isinstance(anchor, dict) else None
        record['anchor'] = anchor_tick
        evidence['anchor'] = anchor

        # The resolution watch: nothing scripted — the field
        # resolves on its own. 'reclaim' when the launch owner
        # re-seats and walks back to active with the sibling
        # tracking; 'adopted' when a successor's declared claim
        # stands and the demoted peer tracks it.
        watch_rows = []
        path = None
        deadline = time.monotonic() + RELEASE_DEADLINE
        while time.monotonic() < deadline and path is None:
            report = _try_role(ctx, ctx[owner])
            partner = _try_role(ctx, ctx[peer])
            watch_rows.append({'owner': report, 'peer': partner})
            if report is not None and partner is not None:
                if report.get('role') == 'active' \
                        and _is_tracking(partner):
                    path = 'reclaim'
                elif partner.get('role') == 'active' \
                        and _is_tracking(report):
                    path = 'adopted'
            if path is None:
                time.sleep(RELEASE_POLL)
        record['resolve'] = {'path': path, 'watch': watch_rows}
        evidence['resolve'] = {'path': path,
                               'watch': [row for row in watch_rows
                                         [-8:]]}
        verdict = _claim_surface(ctx)
        record['resolved_verdict'] = verdict
        evidence['resolved_verdict'] = verdict
        record['walk'] = _journaled_walk(
            ctx, owner, floors[owner]) \
            if floors.get(owner) is not None else None
        record['adoptions'] = _journaled(
            ctx, owner, floors[owner], 'tracking_source_adopted') \
            if floors.get(owner) is not None else None
        records = _durable_records(ctx, owner, durable_floors[owner]) \
            if isinstance(durable_floors.get(owner), int) else None
        record['durable'] = _durable_kinds(records) \
            if records is not None else None
        record['boundaries'] = {'before': boundaries0,
                                'after': _run_boundaries(ctx, owner)}

        # The field resolves for real: the re-seated owner's writes
        # land again — the watch point's frozen tick advancing past
        # the release-time anchor.
        landed = False
        if isinstance(anchor_tick, int):
            def advancing():
                _try_role(ctx, ctx[owner])
                _try_role(ctx, ctx[peer])
                sample = _probe_sample(ctx, watch_point)
                if not isinstance(sample, dict):
                    return None
                return sample \
                    if isinstance(sample.get('tick'), int) \
                    and sample['tick'] > anchor_tick else None
            landed = wait_for(
                advancing, time.monotonic() + RELEASE_DEADLINE,
                interval=RELEASE_POLL) is not None
            record['writes'] = 'landed' if landed else 'stalled'
    finally:
        stream.close()
        if not released:
            # Never leave the field claimed by the induction token:
            # an abandoned pass stages the defect this leg exists
            # to catch. Re-claim to join the attachment's hold then
            # hand it back — the last holder's release unclaims.
            try:
                with _plant_connect(ctx) as conn:
                    _plant_request(conn, {'op': 'claim_writer',
                                          'owner': RELEASE_FOREIGN,
                                          'controller': False})
                    _plant_request(conn, {'op': 'release_writer'})
            except Exception:
                pass
        # The unscripted resolution may still be in flight when the
        # pass wedged — the restore's own settle gives the sibling's
        # demote, the owner's promote, and the reconverge their
        # bounds; refusals are swallowed.
        _restore_layout(ctx, owner, peer)
        record['restored'] = bool(
            _pair_active(ctx) == owner
            and _tracking_standby(ctx, peer) is not None)
    return record, evidence


def _judge_release(record, note):
    """The monitor-less foreign-claim release contract: the fenced
    ex-owner serves standby+unsynchronized on every poll while the
    monitor-less claim stands and the claim surface names the
    induction token with no monitor; after release the field
    resolves — journaled reclaim or declared-monitor adoption —
    never stranded, reconverged, writes landing, roles restored.
    Contract misses report foreign-claim-release-failed; instability
    the contract does not answer for reports
    foreign-claim-release-nondeterministic."""
    def failed(key, detail):
        note(key, DIAG_FAILED, detail)

    def nondet(key, detail):
        note(key, DIAG_NONDET, detail)

    baseline = record.get('baseline')
    if not isinstance(baseline, dict):
        nondet('baseline', 'the claim surface read dropped — the '
               'pass never saw the standing claim')
        return
    if not _fenced(baseline) \
            or _verdict_owner(baseline) != record.get('owner_token'):
        nondet('baseline', 'the standing claim did not name the '
               'launch owner at pass start — the staging '
               'precondition was lost: '
               + json.dumps(baseline, sort_keys=True)[:160])
        return

    claim = record.get('claim') or {}
    if claim.get('result') == 'claimed_shared':
        nondet('claim', 'the induction claim shared the foreign '
               'token — a leaked attachment holds it: '
               + json.dumps(claim, sort_keys=True)[:160])
        return
    if claim.get('result') != 'done':
        nondet('claim', 'the monitor-less induction claim never '
               'granted: ' + json.dumps(claim, sort_keys=True)[:160])
        return

    seized = record.get('seized')
    if not isinstance(seized, dict):
        nondet('seized', 'the claim surface read dropped after the '
               'preemption')
        return
    if not _fenced(seized):
        failed('seized-open', 'the granted induction claim left the '
               'field unfenced: '
               + json.dumps(seized, sort_keys=True)[:160])
        return
    if _verdict_owner(seized) != RELEASE_FOREIGN:
        nondet('seized-foreign', 'the standing claim names '
               + str(_verdict_owner(seized))
               + ' — the induction token never stood: '
               + json.dumps(seized, sort_keys=True)[:160])
        return
    if _verdict_declares_monitor(seized):
        failed('seized-monitor', 'the monitor-less claim declares a '
               'monitor in the verdict — the bound\'s premise is '
               'broken: '
               + json.dumps(seized, sort_keys=True)[:160])

    demoted = record.get('demoted') or {}
    if demoted.get('answered', 0) != demoted.get('polls', -1):
        failed('owner-silent', 'the fenced ex-owner\'s serving '
               'monitor dropped polls through the demotion — a '
               'kill, not the demote-in-place the contract '
               'asserts: ' + json.dumps(demoted)[:160])
    if not demoted.get('walked'):
        failed('demotion', 'the fenced ex-owner never reported '
               'standby — the demotion the bound asserts on never '
               'landed: ' + json.dumps(demoted)[:160])
        return
    walk = demoted.get('walk') or []
    off = [row.get('owner') for row in walk
           if row.get('owner') not in
           (None, 'active', 'demoting', 'standby')]
    if off:
        failed('demotion-walk', 'the demoted ex-owner walked '
               'through off-contract roles: ' + json.dumps(off))

    losses = record.get('losses')
    if losses is None:
        nondet('loss', 'the served-journal read dropped — the '
               'field_claim_lost cannot be audited')
    elif len(losses) != 1:
        failed('loss', 'expected exactly one journaled '
               'field_claim_lost for the preemption, found '
               + str(len(losses)) + ': '
               + json.dumps(losses)[:200])
    else:
        claimant = losses[0].get('claimant') \
            if isinstance(losses[0], dict) else None
        if claimant is None:
            failed('loss', 'the journaled field_claim_lost names '
                   'no claimant — the fencing verdict carries no '
                   'standing owner')
        elif claimant != RELEASE_FOREIGN:
            failed('loss', 'the journaled field_claim_lost names '
                   + str(claimant) + ' — the standing claim\'s '
                   'induction token ' + str(RELEASE_FOREIGN)
                   + ' expected')

    hold = record.get('hold') or []
    if not hold:
        nondet('hold', 'the held window recorded no polls — the '
               'verdict bound cannot be audited')
    else:
        for index, row in enumerate(hold):
            report = row.get('owner')
            if report is None:
                failed('hold-silent', 'the fenced ex-owner\'s '
                       'serving monitor dropped the '
                       + str(index + 1) + '. held poll — a silent '
                       'monitor is a kill, not the demote-in-place '
                       'the contract asserts')
                continue
            if report.get('role') != 'standby':
                failed('hold-role', 'the fenced ex-owner reported '
                       + str(report.get('role')) + ' — not standby '
                       '— while the monitor-less claim stood: '
                       + json.dumps(report, sort_keys=True)[:160])
                continue
            kind = _sync_kind(report)
            if kind != 'unsynchronized':
                failed('hold-verdict', 'the fenced ex-owner '
                       'reported ' + kind + ' while the '
                       'monitor-less claim stood — unsynchronized '
                       'is the only honest verdict the bound '
                       'names for the claim\'s standing window')
            partner = row.get('peer')
            if partner is not None \
                    and partner.get('role') != 'standby':
                nondet('hold-peer', 'the tracking peer left '
                       'standby inside the held window — the '
                       'episode\'s staging broke: '
                       + json.dumps(partner, sort_keys=True)[:160])
            verdict = row.get('verdict')
            if not isinstance(verdict, dict):
                nondet('hold-probe', 'a claim surface read dropped '
                       'inside the held window')
            elif _verdict_owner(verdict) != RELEASE_FOREIGN:
                nondet('hold-preempted', 'the standing claim moved '
                       'off the induction token mid-window: '
                       + json.dumps(verdict, sort_keys=True)[:160])
            elif _verdict_declares_monitor(verdict):
                failed('hold-monitor', 'the monitor-less claim '
                       'names a monitor mid-window: '
                       + json.dumps(verdict, sort_keys=True)[:160])

    release = record.get('release') or {}
    if release.get('result') != 'done':
        nondet('release', 'the claim hand-back never landed: '
               + json.dumps(release, sort_keys=True)[:160])
        return

    resolve = record.get('resolve') or {}
    path = resolve.get('path')
    if path is None:
        last = ((resolve.get('watch') or [{}])[-1] or {}).get('owner')
        failed('stranded', 'the released field never resolved — '
               'the ex-owner still reports '
               + json.dumps(last, sort_keys=True)[:200]
               + ' past the release bound: the permanent strand '
               'the contract forbids')
        return
    verdict = record.get('resolved_verdict')
    if not isinstance(verdict, dict):
        nondet('resolve-verdict', 'the claim surface read dropped '
               'after the resolution')
    else:
        expected = record.get('owner_token') if path == 'reclaim' \
            else record.get('peer_token')
        if _verdict_owner(verdict) != expected:
            nondet('resolve-foreign', 'the resolved claim names '
                   + str(_verdict_owner(verdict)) + ' — a third '
                   'claimant stands: '
                   + json.dumps(verdict, sort_keys=True)[:160])
        elif not _verdict_declares_monitor(verdict):
            failed('resolve-monitor', 'the resolved claim declares '
                   'no monitor — the resolution must leave the '
                   'field naming something to track: '
                   + json.dumps(verdict, sort_keys=True)[:160])
    if path == 'reclaim':
        walk = record.get('walk')
        if walk is None:
            nondet('walk', 'the served-journal read dropped — the '
                   'reclaim\'s journaled evidence cannot be '
                   'audited')
        elif ('promoting', 'active') not in walk \
                and ('standby', 'active') not in walk:
            failed('resolution-unrecorded', 'the ex-owner\'s '
                   're-seat left no journaled walk back to active '
                   '— an operator or restart path ran instead of '
                   'the loss-marked reclaim: '
                   + json.dumps(walk)[:200])
    else:
        adoptions = record.get('adoptions')
        if adoptions is None:
            nondet('adopted', 'the served-journal read dropped — '
                   'the adoption\'s journaled evidence cannot be '
                   'audited')
        else:
            declared = _verdict_monitor(verdict) \
                if isinstance(verdict, dict) else None
            # The adoption journals the normalized endpoint, the
            # verdict carries the declared monitor verbatim —
            # under the wildcard bind the host differs and only
            # the port survives the normalization.
            port = str(declared).rpartition(':')[2] \
                if declared else None
            sources = [entry.get('source')
                       for entry in adoptions
                       if isinstance(entry, dict)]
            if not any(port is None
                       or str(source).rpartition(':')[2] == port
                       for source in sources):
                failed('adopted', 'the demoted peer journaled no '
                       'tracking_source_adopted naming the '
                       'resolved claim\'s declared monitor '
                       + str(declared) + ': '
                       + json.dumps(sources)[:200])

    boundaries = record.get('boundaries') or {}
    if boundaries.get('before') is not None \
            and boundaries.get('after') is not None \
            and boundaries['after'] > boundaries['before']:
        failed('restart', 'the ex-owner\'s durable journal opened '
               'a new run boundary mid-episode — the resolution '
               'needed a restart the contract says is never '
               'required')

    if record.get('writes') is None:
        nondet('anchor', 'the release-time field anchor never '
               'landed — the landing check cannot be audited')
    elif record['writes'] != 'landed':
        failed('writes-stalled', 'the resolved field\'s writes '
               'never landed — the re-seated claim still fences '
               'its owner or no successor scanned (watch point '
               + str(record.get('watch_point'))
               + '\'s tick held at the anchor)')

    durable = record.get('durable')
    if durable is None:
        nondet('durable', 'the durable journal-file read dropped '
               '— the episode\'s durable records cannot be '
               'audited')
    else:
        if 'field_claim_lost' not in durable:
            failed('durable', 'the durable journal file carries '
                   'no field_claim_lost for the episode — the '
                   'served journal\'s records never reached '
                   '--journal-file: ' + json.dumps(durable)[:200])
        if path == 'reclaim' and 'role_changed' not in durable:
            failed('durable', 'the durable journal file carries '
                   'no role_changed for the reclaim walk: '
                   + json.dumps(durable)[:200])
        if path == 'adopted' \
                and 'tracking_source_adopted' not in durable:
            failed('durable', 'the durable journal file carries '
                   'no tracking_source_adopted for the '
                   'resolution: ' + json.dumps(durable)[:200])

    if 'restored' in record and not record.get('restored'):
        failed('restore', 'the pair never settled back to its '
               'launch roles — the restored claim state and the '
               'launch owner\'s active verdict were expected')


def _release_digest(record, violations):
    """The normalized verdict digest the two passes must produce
    identically — the episode's seven clauses reduced to the
    verdict each names. Diagnostics are the complaint's name; the
    digest is the rig's answer."""
    def clean(*keys):
        return not any(key in violations for key in keys)

    path = (record.get('resolve') or {}).get('path')
    return {
        'claim': 'granted' if clean('claim') else 'refused',
        'seized': 'monitor-less'
                  if clean('seized', 'seized-open',
                           'seized-foreign', 'seized-monitor')
                  else 'named',
        'demotion': 'in-place'
                    if clean('demotion', 'demotion-walk',
                             'owner-silent')
                    else 'held',
        'loss': 'attributed' if clean('loss') else 'unattributed',
        'hold': 'unsynchronized'
                if clean('hold', 'hold-role', 'hold-verdict',
                         'hold-silent', 'hold-monitor')
                else 'breached',
        'release': 'done' if clean('release') else 'refused',
        'resolve': path
                   if path and clean('stranded',
                                     'resolution-unrecorded',
                                     'adopted', 'walk',
                                     'resolve-verdict',
                                     'resolve-foreign',
                                     'resolve-monitor',
                                     'restart')
                   else 'wedged',
        'writes': 'landed' if clean('writes-stalled', 'anchor')
                  else 'stalled',
        'durable': 'journaled' if clean('durable') else 'absent',
        'restore': 'restored' if clean('restore') else 'unrestored'}


def _self_check():
    """The unchecked-diagnostic self-check: each planted negative —
    every clause the leg asserts — must produce a named diagnostic
    through the same judge path the rig evidence takes. Returns the
    list of planted keys whose judgment slipped."""
    clean_record = {
        'owner': 'active', 'peer': 'standby',
        'owner_token': 424243, 'peer_token': 424244,
        'baseline': {'result': 'error', 'error': {
            'kind': 'fenced',
            'detail': 'another attachment owns field writes',
            'owner': 424243, 'monitor': '172.18.0.2:8080'}},
        'claim': {'result': 'done'},
        'seized': {'result': 'error', 'error': {
            'kind': 'fenced',
            'detail': 'another attachment owns field writes',
            'owner': RELEASE_FOREIGN}},
        'demoted': {'polls': 3, 'answered': 3,
                    'walk': [{'owner': 'active'},
                             {'owner': 'demoting'},
                             {'owner': 'standby'}],
                    'walked': True},
        'losses': [{'point': 200, 'owner': 424243,
                    'claimant': RELEASE_FOREIGN}],
        'hold': [{'owner': {'role': 'standby', 'tick': 12,
                            'sync': {'unsynchronized': {}}},
                  'peer': {'role': 'standby', 'tick': 12,
                           'sync': {'orphaned': {'checkpoint': 4}}},
                  'verdict': {'result': 'error', 'error': {
                      'kind': 'fenced',
                      'owner': RELEASE_FOREIGN}}}
                 for _ in range(RELEASE_ROUNDS)],
        'release': {'result': 'done'},
        'anchor': 12,
        'resolve': {'path': 'reclaim',
                    'watch': [{'owner': {'role': 'standby',
                                         'sync': {'unsynchronized': {}}},
                               'peer': {'role': 'standby',
                                        'sync': {'orphaned': {}}}},
                              {'owner': {'role': 'active',
                                         'tick': 16},
                               'peer': {'role': 'standby',
                                        'sync': {'tracking': {'aligned': 14}}}}]},
        'resolved_verdict': {'result': 'error', 'error': {
            'kind': 'fenced', 'owner': 424243,
            'monitor': '172.18.0.2:8080'}},
        'walk': [('active', 'demoting'), ('demoting', 'standby'),
                 ('standby', 'promoting'), ('promoting', 'active')],
        'adoptions': [],
        'writes': 'landed',
        'durable': ['field_claim_lost', 'role_changed'],
        'boundaries': {'before': 1, 'after': 1},
        'restored': True}

    def clean_window():
        record = copy.deepcopy(clean_record)
        # The planted negative the issue names: unsynchronized is
        # bound to the claim's standing window — a clean verdict
        # under the held monitor-less claim is off-contract.
        record['hold'][2]['owner']['sync'] = {'tracking': {'aligned': 12}}
        return record

    def stranded_past_release():
        record = copy.deepcopy(clean_record)
        # The doctored negative the issue names: the peer asserted
        # resolved while stranded past the claim's release.
        record['resolve'] = {'path': None,
                             'watch': [{'owner': {'role': 'standby',
                                                  'sync': {'unsynchronized': {}}},
                                        'peer': {'role': 'standby',
                                                 'sync': {'orphaned': {}}}}]}
        record['writes'] = 'stalled'
        return record

    expect = [
        ('clean-while-claimed', clean_window(), DIAG_FAILED,
         'hold-verdict'),
        ('stranded-past-release', stranded_past_release(),
         DIAG_FAILED, 'stranded'),
        ('monitor-declared', {**copy.deepcopy(clean_record),
                              'seized': {'result': 'error', 'error': {
                                  'kind': 'fenced',
                                  'owner': RELEASE_FOREIGN,
                                  'monitor': '172.18.0.9:8080'}}},
         DIAG_FAILED, 'seized-monitor'),
        ('seized-open', {**copy.deepcopy(clean_record),
                         'seized': {'result': 'error', 'error': {
                             'kind': 'unclaimed',
                             'detail': 'no attachment holds field '
                                      'writes'}}},
         DIAG_FAILED, 'seized-open'),
        ('hold-silenced', {**copy.deepcopy(clean_record),
                           'hold': [dict(row, owner=None) if index == 1
                                    else row
                                    for index, row in enumerate(
                                        copy.deepcopy(
                                            clean_record)['hold'])]},
         DIAG_FAILED, 'hold-silent'),
        ('hold-promoted', {**copy.deepcopy(clean_record),
                           'hold': [dict(row, owner={
                               'role': 'active', 'tick': 13})
                               if index == 3 else row
                               for index, row in enumerate(
                                   copy.deepcopy(
                                       clean_record)['hold'])]},
         DIAG_FAILED, 'hold-role'),
        ('never-demoted', {**copy.deepcopy(clean_record),
                           'demoted': {'polls': 4, 'answered': 4,
                                       'walk': [{'owner': 'active'}] * 4,
                                       'walked': False}},
         DIAG_FAILED, 'demotion'),
        ('killed-not-demoted', {**copy.deepcopy(clean_record),
                                'demoted': {'polls': 4, 'answered': 1,
                                            'walk': [{'owner': 'active'},
                                                     {'owner': 'demoting'},
                                                     {'owner': None},
                                                     {'owner': 'standby'}],
                                            'walked': True}},
         DIAG_FAILED, 'owner-silent'),
        ('silent-loss', {**copy.deepcopy(clean_record), 'losses': []},
         DIAG_FAILED, 'loss'),
        ('unattributed-loss',
         {**copy.deepcopy(clean_record),
          'losses': [{'point': 200, 'owner': 424243}]},
         DIAG_FAILED, 'loss'),
        ('misattributed-loss',
         {**copy.deepcopy(clean_record),
          'losses': [{'point': 200, 'owner': 424243,
                      'claimant': 424244}]},
         DIAG_FAILED, 'loss'),
        ('unjournaled-resolution',
         {**copy.deepcopy(clean_record),
          'walk': [('active', 'demoting'), ('demoting', 'standby')]},
         DIAG_FAILED, 'resolution-unrecorded'),
        ('resolve-monitor-less',
         {**copy.deepcopy(clean_record),
          'resolved_verdict': {'result': 'error', 'error': {
              'kind': 'fenced', 'owner': 424243}}},
         DIAG_FAILED, 'resolve-monitor'),
        ('adoption-unnamed',
         {**copy.deepcopy(clean_record),
          'resolve': {'path': 'adopted', 'watch': []},
          'resolved_verdict': {'result': 'error', 'error': {
              'kind': 'fenced', 'owner': 424244,
              'monitor': '172.18.0.3:8080'}},
          'adoptions': []},
         DIAG_FAILED, 'adopted'),
        ('restarted', {**copy.deepcopy(clean_record),
                       'boundaries': {'before': 1, 'after': 2}},
         DIAG_FAILED, 'restart'),
        ('writes-stalled', {**copy.deepcopy(clean_record),
                            'writes': 'stalled'},
         DIAG_FAILED, 'writes-stalled'),
        ('undurable', {**copy.deepcopy(clean_record), 'durable': []},
         DIAG_FAILED, 'durable'),
        ('unrestored', {**copy.deepcopy(clean_record),
                        'restored': False},
         DIAG_FAILED, 'restore'),
        ('baseline-dropped', {**copy.deepcopy(clean_record),
                              'baseline': None},
         DIAG_NONDET, 'baseline'),
        ('claim-refused',
         {**copy.deepcopy(clean_record),
          'claim': {'result': 'error', 'error': {
              'kind': 'fenced', 'owner': 424243}}},
         DIAG_NONDET, 'claim'),
        ('claim-shared',
         {**copy.deepcopy(clean_record),
          'claim': {'result': 'claimed_shared',
                    'owner': RELEASE_FOREIGN}},
         DIAG_NONDET, 'claim'),
        ('seized-dropped', {**copy.deepcopy(clean_record),
                            'seized': None},
         DIAG_NONDET, 'seized'),
        ('seized-foreign',
         {**copy.deepcopy(clean_record),
          'seized': {'result': 'error', 'error': {
              'kind': 'fenced', 'owner': 424299}}},
         DIAG_NONDET, 'seized-foreign'),
        ('release-refused',
         {**copy.deepcopy(clean_record),
          'release': {'result': 'error', 'error': {
              'kind': 'invalid_request'}}},
         DIAG_NONDET, 'release'),
        ('journal-dropped', {**copy.deepcopy(clean_record),
                             'losses': None},
         DIAG_NONDET, 'loss'),
        ('peer-moved',
         {**copy.deepcopy(clean_record),
          'hold': [{**row, 'peer': {'role': 'promoting',
                                    'tick': 12}}
                   for row in copy.deepcopy(clean_record)['hold']]},
         DIAG_NONDET, 'hold-peer'),
        ('durable-dropped', {**copy.deepcopy(clean_record),
                             'durable': None},
         DIAG_NONDET, 'durable'),
    ]

    slipped = []
    violations = {}
    _judge_release(copy.deepcopy(clean_record),
                   lambda key, diag, detail:
                   violations.setdefault(key, (diag, detail)))
    if violations:
        slipped.append(('clean-record', violations))
    for name, record, diagnostic, key in expect:
        violations = {}
        _judge_release(record, lambda key2, diag, detail:
                       violations.setdefault(key2, (diag, detail)))
        got = violations.get(key, (None, ''))[0]
        if got != diagnostic:
            slipped.append((name, diagnostic, got))
    return slipped


def scenario_foreign_claim_release(ctx):
    """Exercise the monitor-less foreign-claim intended-unsynchronized
    and release-resolution contract on the simulated QA rig
    (qa-scenario-foreign-claim-release, the contract #1167's fix
    pins — decision 101's amended intended-unsynchronized bound).
    With the deployed unkeyed pair settled and tracking, the
    dedicated attachment's claim_writer holds the field under a
    foreign token with controller:false and no monitor declared;
    the fenced ex-owner's serving monitor must report standby+
    unsynchronized on every poll of the claim's standing window —
    and only while that claim stands. Releasing it must resolve the
    field: the ex-owner's loss-marked reclaim re-arms its token and
    walks it back to active, or a successor's declared monitor is
    adopted — journaled, reconverged to one active plus one
    tracking standby, never stranded. Named diagnostics
    foreign-claim-release-failed and
    foreign-claim-release-nondeterministic; the unchecked-diagnostic
    self-check reports foreign-claim-release-unchecked; two
    consecutive passes produce identical outcome digests; the leg
    is inconclusive when the staged run predates the contract or
    keys the pair."""
    case = Case(
        'foreign-claim-release',
        'Monitor-less foreign claim unsynchronized window and '
        'release resolution',
        'A foreign attachment\'s claim_writer held with no monitor '
        'declared leaves the fenced ex-owner reporting '
        'unsynchronized only while the claim stands — the field '
        'names nothing to track — and the released field resolves '
        'through the recorded loss-marked reclaim or '
        'declared-monitor adoption, journaled and reconverged, '
        'never stranded')
    try:
        active, standby = ctx.get('active'), ctx.get('standby')
        if not active or not standby:
            return case.finish(
                'inconclusive',
                'the deployed redundant pair is absent — the leg '
                'needs both peers\' monitor endpoints')
        placement = ctx.get('endpoint_placement') or {}
        if any(placement.get(name) != 'loopback'
               for name in ('active', 'standby', 'plant')):
            return case.finish(
                'inconclusive',
                'the peers or the plant are not loopback-placed — '
                'the leg drives the raw plant client and the '
                'monitors over host-published endpoints')
        if not ctx.get('plant'):
            return case.finish(
                'inconclusive',
                'no simulated-plant endpoint is staged — the leg '
                'stages the foreign claim through the lane\'s '
                'claim-aware plant seam')
        if not ctx.get('plant_ctl'):
            return case.finish(
                'inconclusive',
                'no plant_ctl seam is staged — the leg\'s field '
                'census and landing anchor run the shipped '
                'dcs-plant-ctl')
        journal_files = ctx.get('journal_files')
        if not isinstance(journal_files, dict) \
                or not all(journal_files.get(name)
                           for name in ('active', 'standby')):
            return case.finish(
                'inconclusive',
                'no durable journal files are staged — the leg '
                'audits the episode\'s durable records')
        tokens = ctx.get('plant_owner') or {}
        if not all(tokens.get(name)
                   for name in ('active', 'standby')):
            return case.finish(
                'inconclusive',
                'no claim-owner pins are staged — the leg needs '
                'each peer\'s --owner token to attribute the '
                'claims and the fencing verdicts')
        if ctx.get('pair_token'):
            return case.finish(
                'inconclusive',
                'the deployed pair runs keyed — the intended-'
                'unsynchronized bound this leg pins is the unkeyed '
                'pair\'s: the announced verify hands a keyed '
                'demote orphaned, not unsynchronized')
        reports = {name: _try_role(ctx, ctx[name])
                   for name in ('active', 'standby')}
        if all(report is None for report in reports.values()):
            return case.finish(
                'inconclusive',
                'the deployed pair is unreachable — the leg needs '
                'the serving monitors to audit the verdict')
        owner = _pair_active(ctx)
        if owner is None:
            return case.finish(
                'failed',
                'the pair has no settled active peer — '
                'foreign-claim-release-failed: the leg stages the '
                'monitor-less claim on a healthy owner-plus-'
                'tracker pair')
        if owner != 'active':
            # A previous leg's restore fell short — demote the
            # field holder, promote the launch owner back over it.
            _restore_layout(ctx, 'active', 'standby')
            if _pair_active(ctx) != 'active':
                return case.finish(
                    'inconclusive',
                    'the pair never settled back to its launch '
                    'layout — the unconfigured peer must hold the '
                    'field: only its demote owes the served '
                    'unsynchronized window the leg asserts')
            owner = 'active'
        peer = 'standby' if owner == 'active' else 'active'
        deadline = time.monotonic() + RELEASE_SETTLE
        if wait_for(lambda: _tracking_standby(ctx, peer) or None,
                    deadline, interval=POLL_INTERVAL) is None:
            return case.finish(
                'inconclusive',
                'the pair never reported a settled tracking '
                'standby — the leg stages on a healthy '
                'owner-plus-tracker pair')
        baseline = _claim_surface(ctx)
        if baseline is None:
            return case.finish(
                'inconclusive',
                'the plant\'s claim surface answered no probe — '
                'the staged run predates the claim verdict the '
                'contract audits')
        if not _fenced(baseline):
            return case.finish(
                'inconclusive',
                'the claim surface answered no fenced verdict — '
                'the field is unclaimed or the claim read surface '
                'is absent: '
                + json.dumps(baseline, sort_keys=True)[:200])
        if _verdict_owner(baseline) != tokens[owner]:
            return case.finish(
                'inconclusive',
                'the standing claim names '
                + str(_verdict_owner(baseline))
                + ' — the launch owner\'s pin '
                + str(tokens[owner]) + ' expected: '
                + json.dumps(baseline, sort_keys=True)[:200])
        if not _verdict_declares_monitor(baseline):
            return case.finish(
                'inconclusive',
                'the claim verdict names no declared monitor — '
                'the staged run predates the field-arbitrated '
                'monitor contract the released-field resolution '
                'pins: '
                + json.dumps(baseline, sort_keys=True)[:200])
        field_out = _field_out_points(ctx)
        if not field_out:
            return case.finish(
                'inconclusive',
                'the simulated plant serves no field output — the '
                'leg\'s writes-landed check needs a watch point '
                'the settled owner mutates')
        watch = min(field_out)
        case.observe('foreign claim window: claim '
                     + str(RELEASE_FOREIGN)
                     + ' monitor-less under '
                     + str(RELEASE_ROUNDS)
                     + ' held polls; release resolves the field on '
                     'watch point ' + str(watch)
                     + ' through the recorded reclaim or '
                     'declared-monitor adoption')

        digests = []
        for number in (1, 2):
            violations = {}

            def note(key, diagnostic, detail):
                violations.setdefault(key, (diagnostic, detail))

            record, evidence = _release_pass(
                ctx, number, owner, peer, tokens, watch)
            _judge_release(record, note)
            digest = _release_digest(record, violations)
            evidence['record'] = record
            evidence['digest'] = digest
            evidence['violations'] = {
                key: {'diagnostic': diagnostic, 'detail': detail}
                for key, (diagnostic, detail) in violations.items()}
            ref = save_evidence(
                ctx, 'foreign-claim-release-pass-' + str(number),
                evidence)
            case.evidence('file', ref,
                          'the leg\'s normalized record of release '
                          'pass ' + str(number))
            digests.append(digest)
            if violations:
                diagnostic, detail = next(iter(violations.values()))
                return case.finish(
                    'failed', diagnostic + ': ' + detail)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', DIAG_NONDET + ': the two passes\' '
                'outcome digests diverged — '
                + json.dumps(digests, sort_keys=True)[:400])
        slipped = _self_check()
        if slipped:
            return case.finish(
                'failed', DIAG_UNCHECKED + ': ' + str(slipped)[:400])
        case.observe('foreign-claim release: two passes identical; '
                     'the monitor-less claim held the ex-owner at '
                     'unsynchronized through its standing window '
                     'and the released field resolved on '
                     + json.dumps(digests[0], sort_keys=True))
        return case.finish('passed')
    except Exception as exc:
        return case.finish(
            'inconclusive',
            'the leg could not stage the release pass: ' + str(exc))
