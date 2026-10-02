"""The demote_release_stays_released acceptance leg — one module per leg
of the scenario schedule; see qa_lane/scenarios/__init__.py for the
ordering rule and the shared seam."""
from .common import *

# Ordering: the leg cycles the deployed pair's field through a
# documented switch, a voluntary demote, and the released-claim
# hand-back, restoring the pair's launch claim state and roles
# afterwards — so it runs inside the claim-lifecycle cluster, after the
# skew-bound leg, and before the revision legs that need the pair and
# the born seats on their launch layout.
RUNS_AFTER = frozenset({'scenario_claim_skew_bound'})
RUNS_BEFORE = frozenset({'scenario_incompatible_revision',
                         'scenario_model_revision'})


# --------------------------------------------------------------------
# The voluntary-demote released-claim contract — the per-revision lane
# evidence for the contract #1270's fix establishes (WW-LCM-001
# continuity): a voluntary `POST /demote` is a deliberate hand-back of
# the field's write claim, and the just-demoted member's orphan-cycle
# ensure must not re-arm that claim under its own token. The field
# then stands released — no owner serving it, the claim's arbitration
# carrying nothing but the hand-off — until a documented conditional
# path takes it where it stands: the fencing-loss-armed ex-owner's
# bound reclaim (the ownership the *field* ended), a conditional
# promote, or a startup grant.
#
# The defect the contract answers: a routine `POST /demote` on the
# freshly promoted owner released its claim, and the demoted run's own
# orphan-cycle ensure re-armed that claim under its own token within
# about one scan. The pair was left with a claim standing under a
# member reporting `standby` — a run that writes no owner checkpoints,
# because its gate is closed — while the field's own arbitration
# answered every conditional, non-preemptive path `fenced`; and the
# re-arm journaled nothing at all, so the durable trail could not even
# show who re-took the claim. #1270 closes both halves: the release
# sets the `yielded` mark that keeps that run's own probes off (the
# handed-back claim belongs to the successors' conditional paths), and
# a granted re-arm is durable — one `field_claim_rearmed` record per
# contiguous granted streak.
#
# The leg stages the finding's own sequence against the deployed rig,
# on the pair's own field and through the pair's own control plane:
#
# - the pair settles on its launch layout — the launch owner holding
#   the field with its sibling converged `tracking` behind it — and
#   the claim-aware attachment reads the claim under the launch
#   owner's pinned token.
# - `POST /promote` on the tracking peer: its unconditional claim
#   preempts the live owner's, the former owner's next field write
#   meets the fence and demotes it in place (the fencing loss journaled
#   naming the promoting token, the loss mark that arms the reclaim),
#   and the fenced ex-owner converges `tracking` on the successor the
#   standing claim declares.
# - `POST /demote` on the new owner: the deliberate hand-back. The
#   claim it leaves behind is `yielded` and holderless where the
#   demotion keeps it standing and simply hands it to the successors'
#   conditional paths, and the field reads `unclaimed` where the
#   release frees it outright — the leg accepts either, and records
#   which it saw.
# - the hand-off window, polled on both peers' serving monitors, on
#   the dedicated attachment's `probe_writer` verdicts, and on both
#   peers' durable `--journal-file`s: the released claim must not come
#   back under the demoted member's token (its own journal carries no
#   re-arm record, and the claim never stands pinned under that token
#   while the member serves no field), any re-arm that does land must
#   journal by name with its point, the resolution must be
#   attributable in a durable journal — the reclaim-origin promotion
#   or the re-arm record naming who re-took the claim — and the pair
#   must reconverge to one active plus one tracking standby with no
#   operator re-promote and no restart, the reconverged owner's writes
#   reaching the field again.
#
# Named diagnostics: demote-release-rearm-failed tags the contract
# clauses — a re-arm under the demoted member's own token, a claim
# pinned there with no owner serving the field, a resolution no
# durable record names, a role walk the leg never asked for, a pair
# that never reconverged, a field left unwritten, a claim held by a
# third party, an unrestored launch layout — and
# demote-release-rearm-nondeterministic tags the instability the
# contract does not answer for: a refused staging call, an unlanded
# switch, a demotion that never settled, a lost monitor or probe
# read, an unreadable durable journal, a staging that never armed the
# release, and two passes whose digests diverge.
#
# A staged revision that predates the contract reports inconclusive.
# The pre-#1270 build's signature is behavioural and mutually exclusive
# with the contract: the demoted member's own orphan probe re-armed
# the released claim under its own token and — the durable record
# having no such event to write — nothing named it, so the claim never
# left that token across the whole window and the only member that
# ever stood on the field was the demoted one re-taking the claim it
# had handed back. A build carrying the fix cannot present that shape:
# the `yielded` mark keeps that member's own probes off, so a re-arm
# under its token would have to be journaled by name, and any member
# standing on the field afterwards took it through a documented
# conditional path. The doctored case where a re-arm *is* journaled
# under the demoted member's token is therefore the contract's own
# failure, not the pre-contract signature, and the judge names it.
#
# The unchecked-diagnostic self-check replays the judge over planted
# negatives and reports demote-release-rearm-unchecked for any that slip
# through.

DR_SETTLE = 45      # bound on the pair settling to its launch layout
DR_SWITCH = 45      # bound on the tracking peer's promotion landing
DR_FENCE = 30       # bound on the superseded owner demoting in place
DR_CONVERGE = 45    # bound on that ex-owner tracking the successor
DR_DEMOTE = 30      # bound on the voluntary demotion settling standby
DR_WINDOW = 25      # the released-claim window, judged across it
DR_ROUNDS = 6       # answered probe rows the window spans
DR_POLL = 0.4       # cadence polling the pair and the field's claim
DR_RESTORE = 45     # bound on the launch-layout restore
DIAG_FAILED = 'demote-release-rearm-failed'
DIAG_NONDET = 'demote-release-rearm-nondeterministic'
DIAG_UNCHECKED = 'demote-release-rearm-unchecked'

# The sync verdicts a run reports once it has applied a checkpoint from
# the tracked line: `tracking` while the line's owner serves it,
# `orphaned` while it owns no field writes, `reinitialized` after a
# cold restart's reinitialization. None of the three is a field owner
# either — the ex-owner needs one of them applied before its
# orphaned-pull arm can re-land, so a demoted run that never applied
# anything has staged no reclaim.
CONVERGED_SYNC = ('tracking', 'orphaned', 'reinitialized')


def _dr_sync(report):
    """The served StandbySync's variant name — 'unsynchronized' and
    'degraded' are bare strings, the rest single-key objects."""
    sync = (report or {}).get('sync')
    if isinstance(sync, str):
        return sync
    if isinstance(sync, dict) and sync:
        return next(iter(sync))
    return None


def _dr_view(ctx, name):
    """One read of a pair member's serving monitor: the role, the sync
    verdict, the served claim observation, and the tick — the served
    evidence tuple every clause of the contract reads. None when the
    monitor answers nothing: a dropped read is never a view."""
    report = _try_role(ctx, ctx.get(name) or '')
    if report is None:
        return None
    return {'role': report.get('role'),
            'sync': _dr_sync(report),
            'field_claim': report.get('field_claim'),
            'tick': report.get('tick')}


def _dr_probe(ctx):
    """The lane's claim-aware seam: a dedicated attachment's read-only
    `probe_writer` on the plant protocol — the verdict a mutation from
    a third attachment would meet, naming the standing claim's owner
    token and declared monitor while asserting, joining, and releasing
    nothing, so a released field can be read without seizing it.
    `dcs-plant-ctl` exposes no claim verb (it wraps its own mutations
    in its own conditional claim and its releases), so the probe stays
    on the raw client, as the field-claim and claim-reclaim legs' claim
    probes do. None is a lost read, never a verdict."""
    response = _try_plant(ctx, {'op': 'probe_writer'})
    if response is None:
        return None
    error = response.get('error') or {}
    kind = error.get('kind')
    if kind == 'fenced':
        return {'claim': 'held', 'owner': error.get('owner'),
                'monitor': error.get('monitor')}
    if kind == 'unclaimed':
        return {'claim': 'unclaimed', 'owner': None, 'monitor': None}
    if response.get('result') == 'done':
        return {'claim': 'held', 'owner': None, 'monitor': None}
    return {'claim': 'unknown', 'owner': None, 'monitor': None}


def _dr_floor(ctx, name):
    """The newest entry seq a pair member's served `GET /journal`
    carries, or None where the read dropped — the cursor the
    released-claim window's served audit reads above."""
    try:
        _, journal = http_json('GET', ctx[name] + '/journal')
        entries = _journal_list(journal)
    except Exception:
        return None
    return (entries[-1].get('seq') or 0) if entries else 0


def _dr_served(ctx, name, floor):
    """The member's served journal entries above `floor`, or None where
    the read dropped — a dropped read is never an absence of evidence."""
    try:
        _, journal = http_json('GET', ctx[name] + '/journal?since='
                               + str(floor))
    except Exception:
        return None
    return _journal_list(journal)


def _dr_file_floor(path):
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


def _dr_file_entries(path, floor):
    """The durable `--journal-file` entries above `floor`, or None
    while the file cannot be read — the durable half of the window
    audit."""
    if path is None or floor is None:
        return None
    try:
        return [(item.get('entry') or {})
                for item in _journal_entries(path)
                if isinstance((item.get('entry') or {}).get('seq'), int)
                and item['entry']['seq'] > floor]
    except Exception:
        return None


def _dr_events(entries, kind):
    """The `kind` event bodies a served or durable entry slice
    carries — an unread slice reads as None, never as empty."""
    if entries is None:
        return None
    out = []
    for entry in entries:
        event = (entry or {}).get('event') or {}
        if isinstance(event.get(kind), dict):
            out.append(event[kind])
    return out


def _dr_walk(entries):
    """The journaled `role_changed` transitions as (from, to, origin)
    tuples — the walk attribution the contract's clauses read."""
    return [(change.get('from'), change.get('to'), change.get('origin'))
            for change in _dr_events(entries, 'role_changed') or []]


def _dr_settle(ctx, name, want, bound, poll=None):
    """Poll a member's serving monitor until its reported role is
    `want`; returns the matching view, else None."""
    accepted = []

    def found():
        view = _dr_view(ctx, name)
        if view is not None and view.get('role') == want:
            accepted.append(view)
            return view
        return None

    wait_for(found, time.monotonic() + bound,
             interval=poll or DR_POLL)
    return accepted[-1] if accepted else None


def _dr_converged(ctx, name, bound):
    """Poll a member's serving monitor until it reports a converged
    standby — `standby` with a tracking/orphaned/reinitialized verdict,
    the applied-checkpoint evidence the ex-owner's orphaned-pull arm
    reads; returns the matching view, else None."""
    accepted = []

    def found():
        view = _dr_view(ctx, name)
        if view is not None and view.get('role') == 'standby' \
                and view.get('sync') in CONVERGED_SYNC:
            accepted.append(view)
            return view
        return None

    wait_for(found, time.monotonic() + bound, interval=DR_POLL)
    return accepted[-1] if accepted else None


def _dr_restore(ctx, owner, peer, deadline):
    """Best-effort launch-layout restore inside one bound: put the
    launch owner back on the field — the documented demote/promote
    order, which claims unconditionally — and wait for the pair's
    tracking posture with the field's claim standing under the launch
    owner's pinned token. Cleanup, never the contract the leg
    judges."""
    tokens = ctx.get('plant_owner') or {}
    while time.monotonic() < deadline:
        current = _pair_active(ctx)
        if current == peer:
            _settle_call(ctx[peer] + '/demote')
        elif current is None:
            # No peer owns the field: a promotion on either converged
            # member takes it back through the unconditional claim.
            for name in (owner, peer):
                _settle_call(ctx[name] + '/promote')
        else:
            claim = _dr_probe(ctx)
            if claim is None or claim.get('claim') == 'held':
                if _tracking_standby(ctx, peer) is not None:
                    return True
            else:
                # The launch owner owns the gate but the field carries
                # no claim for it: cycle the documented order so a
                # promotion re-takes the field.
                _settle_call(ctx[peer] + '/demote')
                _settle_call(ctx[owner] + '/promote')
        time.sleep(DR_POLL)
    return _pair_active(ctx) == owner \
        and _tracking_standby(ctx, peer) is not None \
        and (_dr_probe(ctx) or {}).get('owner') in (None, tokens.get(owner))


def _dr_pass(ctx, number, owner, peer, tokens, journal_files):
    """One pass over the voluntary-demote released-claim contract:
    settle the pair on its launch layout, promote the tracking peer so
    the launch owner fences and demotes in place, demote the new
    owner so its claim is handed back, and watch the released claim
    through both peers' serving monitors, a dedicated attachment's
    probe verdicts, and both durable journals until a documented
    conditional path takes the field and the pair reconverges. Returns
    (record, evidence); an aborted stage simply leaves its later keys
    absent."""
    record = {'pass': number, 'owner': owner, 'peer': peer,
              'tokens': {owner: tokens[owner], peer: tokens[peer]}}
    evidence = {'pass': number, 'owner': owner, 'peer': peer}
    owner_base, peer_base = ctx[owner], ctx[peer]

    try:
        # --- the launch layout -------------------------------------
        record['settled'] = wait_for(
            lambda: _pair_active(ctx) == owner
                    and _tracking_standby(ctx, peer) or None,
            time.monotonic() + DR_SETTLE, interval=DR_POLL) is not None
        if not record['settled']:
            record['stage_error'] = (
                'the pair never settled on its launch layout — '
                + owner + ' must own the field with ' + peer
                + ' converged tracking behind it: '
                + json.dumps({'owner': _dr_view(ctx, owner),
                              'peer': _dr_view(ctx, peer)})[:300])
            return record, evidence
        record['baseline'] = _dr_probe(ctx)
        evidence['baseline'] = record['baseline']
        record['pair_before'] = {'owner': _dr_view(ctx, owner),
                                 'peer': _dr_view(ctx, peer)}
        record['watch_point'] = (_field_out_points(ctx) or [None])[0]
        record['watch_before'] = _field_sample(
            ctx, record['watch_point']) if record['watch_point'] else None

        # --- the switch: promote the tracking peer ------------------
        record['switch_floor'] = {name: _dr_file_floor(
            journal_files.get(name)) for name in (owner, peer)}
        record['switch_served'] = {name: _dr_floor(ctx, name)
                                   for name in (owner, peer)}
        promoted = None
        refusals = []
        bound = time.monotonic() + DR_SWITCH
        while time.monotonic() < bound and promoted is None:
            status, body = _settle_call(peer_base + '/promote')
            if status == 200:
                promoted = body
            else:
                refusals.append({'status': status, 'body': body})
                time.sleep(DR_POLL)
        record['promote'] = {'report': promoted, 'refusals': refusals[:4]}
        if promoted is None:
            record['stage_error'] = (
                'POST /promote on the tracking peer never landed — it '
                'never converged for the switch: '
                + json.dumps(refusals[-2:])[:300])
            return record, evidence
        record['new_owner'] = _dr_settle(ctx, peer, 'active', DR_FENCE)
        if record['new_owner'] is None:
            record['stage_error'] = (
                'the promoted peer never settled active holding the '
                'field: ' + json.dumps(_dr_view(ctx, peer))[:200])
            return record, evidence
        record['fenced'] = _dr_settle(ctx, owner, 'standby', DR_FENCE)
        if record['fenced'] is None:
            record['stage_error'] = (
                'the superseded owner never demoted in place on the '
                'promotion\'s fence: ' + json.dumps(_dr_view(ctx, owner))
                [:200])
            return record, evidence
        record['converged'] = _dr_converged(ctx, owner, DR_CONVERGE)
        record['promoted_probe'] = _dr_probe(ctx)
        evidence['promote'] = record['promote']
        evidence['new_owner'] = record['new_owner']
        evidence['fenced'] = record['fenced']
        evidence['converged'] = record['converged']
        evidence['promoted_probe'] = record['promoted_probe']
        evidence['switch_durable'] = {
            name: _dr_file_entries(journal_files.get(name),
                                   record['switch_floor'][name])
            for name in (owner, peer)}

        # --- the voluntary demote: the deliberate hand-back ---------
        status, demote = _settle_call(peer_base + '/demote')
        record['demote'] = {'status': status, 'report': demote}
        evidence['demote'] = record['demote']
        if status != 200 or (demote or {}).get('role') != 'demoting':
            record['stage_error'] = (
                'POST /demote on the new owner answered ' + str(status)
                + ' ' + json.dumps(demote)[:300])
            return record, evidence
        record['released'] = _dr_settle(ctx, peer, 'standby', DR_DEMOTE)
        record['release_probe'] = _dr_probe(ctx)
        evidence['released'] = record['released']
        evidence['release_probe'] = record['release_probe']
        # The audit cursors, taken where the demotion settled: the
        # window above them is the released claim's alone.
        record['floor'] = {name: _dr_file_floor(journal_files.get(name))
                           for name in (owner, peer)}
        record['served_floor'] = {name: _dr_floor(ctx, name)
                                  for name in (owner, peer)}

        # --- the hand-off window -----------------------------------
        rows = []
        collected = 0
        bound = time.monotonic() + DR_WINDOW
        while time.monotonic() < bound:
            claim = _dr_probe(ctx)
            demoted_view = _dr_view(ctx, peer)
            ex_owner_view = _dr_view(ctx, owner)
            row = {'claim': (claim or {}).get('claim'),
                   'owner': (claim or {}).get('owner'),
                   'monitor': (claim or {}).get('monitor'),
                   'demoted': demoted_view,
                   'ex_owner': ex_owner_view}
            rows.append(row)
            if claim is not None and demoted_view is not None \
                    and ex_owner_view is not None:
                collected += 1
                if collected >= DR_ROUNDS and _dr_resolved(
                        rows, owner, peer, tokens):
                    break
            time.sleep(DR_POLL)
        record['rows'] = rows
        answered = [row for row in rows
                    if row.get('claim') in ('held', 'unclaimed')
                    and row.get('demoted') and row.get('ex_owner')]
        record['answered'] = len(answered)
        record['owners'] = sorted({row.get('owner') for row in rows
                                   if row.get('claim') == 'held'
                                   and row.get('owner') is not None})
        record['final'] = answered[-1] if answered else None
        record['watch_after'] = _field_sample(
            ctx, record['watch_point']) if record['watch_point'] else None
        evidence['rows'] = rows
        evidence['answered'] = record['answered']
        evidence['owners'] = record['owners']
        evidence['final'] = record['final']
        evidence['watch_point'] = record['watch_point']
        evidence['watch_before'] = record['watch_before']
        evidence['watch_after'] = record['watch_after']

        # --- the durable windows -----------------------------------
        record['durable'] = {
            name: _dr_file_entries(journal_files.get(name),
                                   (record.get('floor') or {}).get(name))
            for name in (owner, peer)}
        record['served'] = {
            name: _dr_served(ctx, name,
                             (record.get('served_floor') or {}).get(name))
            for name in (owner, peer)}
        evidence['durable'] = record['durable']
        evidence['served'] = record['served']
    finally:
        record['restored'] = _dr_restore(ctx, owner, peer,
                                         time.monotonic() + DR_RESTORE)
        record['restored_owner'] = _pair_active(ctx)
        record['restored_probe'] = _dr_probe(ctx)
    evidence['restored'] = record.get('restored')
    evidence['restored_owner'] = record.get('restored_owner')
    evidence['restored_probe'] = record.get('restored_probe')
    return record, evidence


def _dr_resolved(rows, owner, peer, tokens):
    """Whether the window's own rows already show the contract's
    terminal shape: the field's claim under the member that stands on
    it, the other member a tracking standby. The loop stops on it so
    the digest reads the resolution rather than the window's tail."""
    owner_token, peer_token = tokens[owner], tokens[peer]
    for row in reversed(rows):
        demoted, ex_owner = row.get('demoted') or {}, row.get('ex_owner') \
            or {}
        if row.get('claim') != 'held':
            continue
        if row.get('owner') == owner_token \
                and ex_owner.get('role') == 'active' \
                and demoted.get('role') == 'standby' \
                and demoted.get('sync') == 'tracking':
            return True
        if row.get('owner') == peer_token \
                and demoted.get('role') == 'active' \
                and ex_owner.get('role') == 'standby':
            return True
    return False


def _dr_judge(record, note):
    """Audit one pass's record — replayable, so the self-check can hand
    it planted negatives. `note(key, diagnostic, detail)` records each
    clause the record violates: DIAG_FAILED tags the contract clauses
    and DIAG_NONDET the instability the contract does not answer for.
    Each stage's clauses are guarded by that stage's own evidence, so
    a stage that never ran leaves its later keys unread rather than
    reporting an absence as a failure."""
    def failed(key, detail):
        note(key, DIAG_FAILED, detail)

    def nondet(key, detail):
        note(key, DIAG_NONDET, detail)

    tokens = record.get('tokens') or {}
    owner, peer = record.get('owner'), record.get('peer')
    if record.get('stage_error') is not None:
        nondet('stage', 'the staging never completed: '
               + str(record['stage_error']))
        return
    if not record.get('settled'):
        failed('settle', 'the pair never settled on its launch layout — '
               + str(owner) + ' must own the field with ' + str(peer)
               + ' converged tracking behind it')
    baseline = record.get('baseline')
    if baseline is None:
        nondet('probe', 'the claim-aware attachment\'s first probe never '
               'answered — the field\'s claim posture was never read')
    elif baseline.get('claim') != 'held' \
            or baseline.get('owner') != tokens.get(owner):
        nondet('probe', 'the claim-aware attachment did not read the '
               'launch claim under the launch owner\'s pinned token: '
               + json.dumps(baseline)[:200])
    if record.get('new_owner') is None:
        nondet('switch', 'the promoted peer never settled active holding '
               'the field — the preemption the demote releases never '
               'stood')
    promoted = record.get('promoted_probe')
    if record.get('new_owner') is not None:
        if promoted is None:
            nondet('probe', 'the claim-aware attachment\'s post-promotion '
                   'probe never answered')
        elif promoted.get('claim') != 'held' \
                or promoted.get('owner') != tokens.get(peer):
            nondet('probe', 'the promoted claim does not stand under the '
                   'promoted peer\'s pinned token: '
                   + json.dumps(promoted)[:200])
    if record.get('fenced') is None:
        failed('fence', 'the superseded owner never demoted in place on '
               'the promotion\'s fence — the fencing-loss arm the '
               'released claim hands the field back to was never set')
    switch = record.get('switch_durable') or {}
    for name, entries in switch.items():
        losses = _dr_events(entries, 'field_claim_lost')
        if entries is None:
            nondet('durable', 'the ' + str(name) + ' peer\'s durable '
                   'journal never read for the switch\'s loss audit')
            continue
        if name != owner:
            continue
        if not losses:
            failed('loss-journaled', 'the superseded owner\'s durable '
                   'journal carries no field_claim_lost — the '
                   'preemption that armed its reclaim left no durable '
                   'record')
        elif len(losses) > 1:
            failed('loss-journaled', 'the superseded owner\'s durable '
                   'journal carries ' + str(len(losses))
                   + ' field_claim_lost records above the switch — one '
                   'held claim loses it once')
        elif losses[0].get('claimant') != tokens.get(peer):
            failed('loss-attribution', 'the superseded owner\'s '
                   'field_claim_lost names claimant '
                   + str(losses[0].get('claimant')) + ', not the '
                   'promoted peer\'s owner token '
                   + str(tokens.get(peer)))
        walk = _dr_walk(entries)
        if not any(row[1] == 'standby' and row[2] == 'fenced'
                   for row in walk):
            failed('demotion-journaled', 'the superseded owner\'s '
                   'journal carries no fenced-origin walk into standby — '
                   'the demote-in-place never landed: '
                   + json.dumps(walk)[:200])
    if record.get('converged') is None:
        nondet('converge', 'the fenced ex-owner never converged on the '
               'successor it must re-claim from — it applied no '
               'checkpoint, so the orphaned-pull arm never re-armed')

    # --- the demotion -----------------------------------------------
    demote = record.get('demote') or {}
    if demote.get('status') != 200 \
            or (demote.get('report') or {}).get('role') != 'demoting':
        failed('demote', 'POST /demote on the new owner answered '
               + json.dumps(demote)[:300])
    if record.get('released') is None:
        nondet('release', 'the demoted new owner never settled standby — '
               'the voluntary hand-back never settled')
    release = record.get('release_probe')
    if record.get('released') is not None:
        if release is None:
            nondet('probe', 'the claim-aware attachment\'s post-demote '
                   'probe never answered')
        elif release.get('claim') not in ('held', 'unclaimed'):
            nondet('probe', 'the post-demote probe answered outside the '
                   'claim vocabulary: ' + json.dumps(release)[:200])
        elif release.get('claim') == 'held' \
                and release.get('owner') not in (None, tokens.get(peer)):
            nondet('probe', 'the demote left the claim under a token '
                   'neither pair member owns: '
                   + json.dumps(release)[:200])

    rows = record.get('rows')
    if not rows:
        nondet('window', 'the released-claim window collected no probe '
               'row — the hand-back was never observed')
        return
    if not record.get('answered'):
        nondet('starved', 'the released-claim window answered no '
               'complete row — both monitors and the claim probe must '
               'answer together')
    for row in rows:
        if row.get('claim') not in ('held', 'unclaimed'):
            continue
        demoted = row.get('demoted') or {}
        ex_owner = row.get('ex_owner') or {}
        if demoted.get('role') in ('promoting', 'active') \
                and ex_owner.get('role') == 'active':
            failed('dual-owner', 'both peers stood on the field at once — '
                   'the demoted member took it back while the ex-owner '
                   'served it: ' + json.dumps(row)[:300])
    # The demoted member kept serving: a run that keeps its served tick
    # advancing while it reports `standby` is the quiesced serving
    # member the contract describes, not a container that stopped
    # answering.
    demoted_ticks = [tick for tick in
                     ((row.get('demoted') or {}).get('tick')
                      for row in rows) if isinstance(tick, int)]
    if len(demoted_ticks) < 2 or demoted_ticks[-1] <= demoted_ticks[0]:
        nondet('starved', 'the demoted member\'s served tick never advanced '
               'across the hand-off window — a standby that stopped '
               'serving cannot be told from one that never came back: '
               + json.dumps(demoted_ticks)[:200])
    final = record.get('final')
    if final is None:
        nondet('window', 'the released-claim window answered no complete '
               'row to judge the field\'s terminal claim against')
    else:
        demoted = final.get('demoted') or {}
        ex_owner = final.get('ex_owner') or {}
        active = None
        if final.get('claim') == 'unclaimed':
            failed('claim-open', 'the field stood unclaimed at the end of '
                   'the hand-off window — no documented conditional path '
                   'took the released claim: ' + json.dumps(final)[:300])
        elif final.get('claim') == 'held':
            if final.get('owner') == tokens.get(peer):
                active = peer if demoted.get('role') == 'active' else None
                if active is None:
                    failed('claim-pinned', 'the released claim stood under '
                           'the demoted member\'s own token to the end of '
                           'the window while that member served no field '
                           'writes — no documented conditional path took '
                           'the hand-back it released: '
                           + json.dumps(final)[:300])
            elif final.get('owner') == tokens.get(owner):
                active = owner if ex_owner.get('role') == 'active' else None
                if active is None:
                    failed('claim-orphan', 'the claim transferred to the '
                           'fencing-loss ex-owner\'s token while that peer '
                           'never stood on the field — the re-took claim '
                           'has no owner serving it: '
                           + json.dumps(final)[:300])
            else:
                failed('claim-foreign', 'the field\'s claim stands under a '
                       'token neither pair member owns — '
                       + json.dumps(final)[:200])
        if active is not None:
            standby = demoted if active == owner else ex_owner
            if standby.get('role') != 'standby':
                failed('pair', 'the member that did not take the field is '
                       'not a standby: ' + json.dumps(standby)[:200])
            elif standby.get('sync') != 'tracking':
                failed('pair', 'the pair did not reconverge to one active '
                       'plus one tracking standby — the other member '
                       'reports ' + str(standby.get('sync')) + ': '
                       + json.dumps(final)[:300])

    # --- the durable trail -----------------------------------------
    durable = record.get('durable') or {}
    served = record.get('served') or {}
    for name in (owner, peer):
        for half, entries in (('durable', durable.get(name)),
                              ('served', served.get(name))):
            if entries is None:
                nondet('durable' if half == 'durable' else 'starved',
                       'the ' + str(name) + ' peer\'s ' + half
                       + ' journal never read for the released-claim '
                       'window')
                continue
            rearms = _dr_events(entries, 'field_claim_rearmed') or []
            if name == peer and rearms:
                failed('rearm-journaled', 'the demoted member\'s journal '
                       'records a field_claim_rearmed inside the released-'
                       'claim window — its own orphan machinery re-armed '
                       'the claim it deliberately handed back: '
                       + json.dumps(rearms)[:300])
            for rearm in rearms:
                if not isinstance(rearm.get('point'), int):
                    failed('rearm-unnamed', 'a field_claim_rearmed record '
                           'names no field point — the durable trail '
                           'cannot say what it re-armed: '
                           + json.dumps(rearm)[:200])
            walk = _dr_walk(entries)
            if any(row[2] == 'request' and row[1] in ('promoting', 'active')
                   for row in walk):
                failed('operator-promote', 'a peer journaled a '
                       'request-origin promotion into the field — the '
                       'hand-off resolved through an operator re-promote '
                       'the leg never issued: '
                       + json.dumps(walk)[-300:])
    # The attribution clause: whoever ended on the field took it through
    # a documented path the durable record names — the reclaim-origin
    # promotion the fencing-loss arm drives, or the re-arm record the
    # grant journals.
    final = record.get('final') or {}
    active_name = _dr_field_holder(record)
    if active_name is not None and durable.get(active_name) is not None:
        walk = _dr_walk(durable[active_name])
        rearms = _dr_events(durable[active_name],
                            'field_claim_rearmed') or []
        claimed = any(row[1] in ('promoting', 'active') for row in walk)
        if claimed and not rearms \
                and not any(row[1] == 'promoting'
                            and row[2] in ('reclaim', 'failover')
                            for row in walk):
            failed('unattributed', 'the member that took the field '
                   'journaled no re-arm and no reclaim- or failover-origin '
                   'promotion naming it — the durable trail cannot show '
                   'who re-took the claim: ' + json.dumps(walk)[:300])

    # --- the field writes again -------------------------------------
    before, after = record.get('watch_before'), record.get('watch_after')
    if record.get('watch_point') is None:
        nondet('field', 'the shipped tool\'s census named no field out '
               'point — the field-liveness witness could not be read')
    elif before is None or after is None:
        nondet('field', 'the watched field point never answered a stored '
               'sample through the shipped tool')
    elif not isinstance(after.get('tick'), int) \
            or not isinstance(before.get('tick'), int) \
            or after['tick'] <= before['tick']:
        failed('field-frozen', 'the watched field output\'s tick never '
               'advanced across the hand-off window — the reconverged '
               'owner\'s writes never reached the field: '
               + json.dumps({'before': before, 'after': after})[:300])

    # --- the launch layout restored ---------------------------------
    if record.get('restored') is not True:
        failed('restore', 'the pair\'s launch claim state and roles did '
               'not restore after the staged episode')
    elif record.get('restored_owner') not in (None, owner):
        failed('restore', 'the restore left a field-owning member other '
               'than the launch owner ' + str(owner) + ': '
               + str(record.get('restored_owner')))
    elif (record.get('restored_probe') or {}).get('claim') == 'unclaimed':
        failed('restore', 'the restore left the field carrying no claim — '
               'the launch owner does not hold its own field again: '
               + json.dumps(record.get('restored_probe'))[:200])


def _dr_field_holder(record):
    """The pair member whose token the field's claim stands under while
    that same member reports the field-owning role — the member the
    released claim's documented path put on the field — or None where
    the terminal row names no such member."""
    final = record.get('final') or {}
    if final.get('claim') != 'held':
        return None
    tokens = record.get('tokens') or {}
    for name, key in ((record.get('peer'), 'demoted'),
                      (record.get('owner'), 'ex_owner')):
        if final.get('owner') == tokens.get(name) \
                and (final.get(key) or {}).get('role') == 'active':
            return name
    return None


def _dr_digest(record, violations):
    """The pass's normalized verdict record — identical across clean
    passes; each field carries the recorded disposition only while no
    violation names it."""
    def clean(*keys):
        return not any(key in violations for key in keys)
    final = record.get('final') or {}
    tokens = record.get('tokens') or {}
    peer = record.get('peer')
    if final.get('claim') == 'held' \
            and final.get('owner') not in (None, tokens.get(peer)):
        claim = 'moved'
    elif final.get('claim') == 'held':
        claim = 'pinned'
    elif final.get('claim') == 'unclaimed':
        claim = 'open'
    else:
        claim = 'unseen'
    return {
        'staging': 'armed'
            if clean('settle', 'fence', 'loss-journaled',
                     'loss-attribution', 'demotion-journaled', 'demote')
            else 'unstaged',
        'release': _dr_release_word(record),
        'rearm': 'journaled'
            if clean('rearm-journaled', 'rearm-unnamed') else 'absent',
        'claim': claim,
        'resolution': 'unattributed' if not clean('unattributed') else (
            'reclaim' if _dr_reclaimed(record) else
            'successor' if _dr_taken(record) else 'none'),
        'pair': 'converged'
            if clean('pair', 'dual-owner', 'claim-pinned', 'claim-orphan',
                     'claim-foreign', 'claim-open', 'field-frozen')
            else 'diverged',
        'journal': 'clean'
            if clean('unattributed', 'operator-promote') else 'breached',
        'reads': 'complete'
            if clean('stage', 'probe', 'starved', 'durable', 'window',
                     'converge', 'switch', 'release', 'field')
            else 'partial',
        'roles': 'restored' if clean('restore') else 'unrestored'}


def _dr_release_word(record):
    """The demote's own release shape as the claim-aware attachment
    read it: `yielded` where the demotion kept the claim standing and
    handed it to the successors' conditional paths, `freed` where the
    release opened the documented unclaimed window, `unseen` where the
    probe never answered it."""
    release = record.get('release_probe')
    if not release:
        return 'unseen'
    return 'yielded' if release.get('claim') == 'held' else 'freed'


def _dr_reclaimed(record):
    """Whether a peer journaled the reclaim-origin promotion the
    fencing-loss arm drives — the documented path the released claim
    hands itself to when the ex-owner's bound grant lands."""
    for entries in (record.get('durable') or {}).values():
        if any(row[1] == 'promoting' and row[2] == 'reclaim'
               for row in _dr_walk(entries or [])):
            return True
    return False


def _dr_taken(record):
    """Whether any peer journaled a documented non-operator walk into
    field ownership inside the released-claim window."""
    for entries in (record.get('durable') or {}).values():
        if any(row[1] in ('promoting', 'active')
               and row[2] in ('reclaim', 'failover')
               for row in _dr_walk(entries or [])):
            return True
    return False


def _dr_self_check():
    """The leg's unchecked-diagnostic self-test: replay the judge over
    each planted negative it must name — the issue's named doctored
    record (the release asserted as staying released while the
    demoted peer's token still holds the claim, journaled by name so
    it is the contract's failure and not the pre-contract signature),
    the re-arm that lands with no point named, the resolution no
    durable record names, an operator re-promote, the pair that never
    reconverged, a frozen field, a third party's claim, the
    unrestored launch layout, and the instability shapes — and require
    the judge to note each. A silent judge returns the negative names
    it let through."""
    slipped = []

    def audit(record):
        found = {}
        _dr_judge(record, lambda key, diagnostic, detail:
                  found.setdefault(key, diagnostic))
        return found

    def expect(name, mutate, diagnostic=DIAG_FAILED):
        record = _dr_clean_record()
        mutate(record)
        if diagnostic not in audit(record).values():
            slipped.append(name)

    if audit(_dr_clean_record()):
        slipped.append('clean-overstrict')

    # The issue's named doctored negative: the release asserted as
    # staying released while the demoted peer's token still holds the
    # claim — the served surfaces and the demoted peer's role read the
    # demotion's own outcome, and only the claim probe and the durable
    # re-arm record say the claim came back.
    expect('release-stays-released-while-the-demoted-token-holds',
           lambda record: (record['final'].update(
               {'claim': 'held', 'owner': record['tokens']['standby']}),
               record['durable']['standby'][0].update(
                   {'event': {'field_claim_rearmed': {'point': 100}}})))
    expect('rearm-lands-silently', lambda record:
           record['durable']['standby'][0].update(
               {'event': {'field_claim_rearmed': {}}}))
    expect('claim-pinned-with-no-owner', lambda record:
           record['final'].update({'owner': record['tokens']['standby']}))
    expect('resolution-unattributed', lambda record:
           record['durable'].__setitem__('active', [
               {'seq': 7, 'tick': 26, 'event': {'role_changed': {
                   'from': 'standby', 'to': 'promoting',
                   'origin': 'fenced'}}},
               {'seq': 8, 'tick': 27, 'event': {'role_changed': {
                   'from': 'promoting', 'to': 'active',
                   'origin': 'fenced'}}}]))
    expect('operator-repromoted', lambda record:
           record['durable']['active'].append(
               {'seq': 6, 'tick': 6, 'event': {'role_changed': {
                   'from': 'standby', 'to': 'promoting',
                   'origin': 'request'}}}))
    expect('pair-never-reconverged', lambda record:
           record['final']['demoted'].update({'sync': 'orphaned'}))
    expect('both-peers-on-the-field', lambda record:
           record['rows'][3]['demoted'].update({'role': 'active'}))
    expect('field-left-unwritten', lambda record:
           record['watch_after'].update({'tick': 40}))
    expect('third-party-claim', lambda record:
           record['final'].update({'owner': 6162}))
    expect('claim-stood-unclaimed', lambda record:
           record['final'].update({'claim': 'unclaimed', 'owner': None}))
    expect('claim-with-no-owner-serving', lambda record:
           record['final'].update({'ex_owner': {
               'role': 'standby', 'sync': 'orphaned',
               'field_claim': 'held', 'tick': 61}}))
    expect('fenced-owner-never-demoted', lambda record:
           record.update({'fenced': None}))
    expect('demote-refused', lambda record:
           record.update({'demote': {'status': 409, 'report': {
               'error': 'no_tracking_source'}}}))
    expect('pair-never-settled', lambda record:
           record.update({'settled': False}))
    expect('loss-unjournaled', lambda record:
           record['switch_durable']['active'].clear())
    expect('loss-misattributed', lambda record:
           record['switch_durable']['active'][0]['event'][
               'field_claim_lost'].update({'claimant': 99}))
    expect('field-frozen-with-samples', lambda record:
           record.update({'watch_after': {'tick': 40,
                                          'quality': 'good'}}))
    expect('layout-unrestored', lambda record:
           record.update({'restored': False}))
    expect('restore-left-a-foreign-owner', lambda record:
           record.update({'restored_owner': 'standby'}))
    expect('restore-left-the-field-unclaimed', lambda record:
           record.update({'restored_probe': {
               'claim': 'unclaimed', 'owner': None, 'monitor': None}}))

    # The instability shapes must report nondeterministic: a refused
    # staging call, a switch that never landed, a lost probe, an
    # unreadable durable journal, a starved window, an ex-owner that
    # never converged, a field point the shipped tool's census never
    # named.
    expect('stage-refused', lambda record:
           record.update({'stage_error': 'docker run failed'}),
           DIAG_NONDET)
    expect('promotion-never-settled', lambda record:
           record.update({'new_owner': None}), DIAG_NONDET)
    expect('probe-never-answered', lambda record:
           record.update({'baseline': None}), DIAG_NONDET)
    expect('baseline-not-the-launch-claim', lambda record:
           record['baseline'].update({'owner': 99}), DIAG_NONDET)
    expect('promoted-claim-misattributed', lambda record:
           record['promoted_probe'].update({'owner': 99}), DIAG_NONDET)
    expect('ex-owner-never-converged', lambda record:
           record.update({'converged': None}), DIAG_NONDET)
    expect('demotion-never-settled', lambda record:
           record.update({'released': None}), DIAG_NONDET)
    expect('release-probe-never-answered', lambda record:
           record.update({'release_probe': None}), DIAG_NONDET)
    expect('window-starved', lambda record:
           record.update({'rows': [], 'answered': 0, 'final': None,
                          'owners': []}), DIAG_NONDET)
    expect('demoted-member-stopped-serving', lambda record:
           [row['demoted'].__setitem__('tick', 40)
            for row in record['rows']], DIAG_NONDET)
    expect('durable-unreadable', lambda record:
           record.update({'durable': {'active': None, 'standby': None},
                          'served': {'active': None, 'standby': None}}),
           DIAG_NONDET)
    expect('no-field-point-to-watch', lambda record:
           record.update({'watch_point': None}), DIAG_NONDET)
    return slipped


def _dr_clean_record():
    """A pass record for the contract's own shape: the released claim
    stood yielded under the demoted member's token, the
    fencing-loss-armed ex-owner's bound reclaim took it inside the
    window, and the pair reconverged to the launch owner's active plus
    the demoted member's tracking standby."""
    return {
        'pass': 1,
        'owner': 'active',
        'peer': 'standby',
        'tokens': {'active': 424243, 'standby': 424244},
        'settled': True,
        'baseline': {'claim': 'held', 'owner': 424243,
                     'monitor': 'ctrl-a:8080'},
        'pair_before': {'owner': {'role': 'active', 'sync': None,
                                  'field_claim': 'held', 'tick': 100},
                        'peer': {'role': 'standby', 'sync': 'tracking',
                                 'field_claim': 'held', 'tick': 96}},
        'watch_point': 100,
        'watch_before': {'value': {'float': 1.5}, 'quality': 'good',
                         'tick': 40},
        'watch_after': {'value': {'float': 1.5}, 'quality': 'good',
                        'tick': 96},
        'switch_floor': {'active': 3, 'standby': 2},
        'switch_served': {'active': 3, 'standby': 2},
        'promote': {'report': {'role': 'promoting'}, 'refusals': []},
        'new_owner': {'role': 'active', 'sync': None,
                      'field_claim': 'held', 'tick': 12},
        'fenced': {'role': 'standby', 'sync': 'tracking',
                   'field_claim': 'held', 'tick': 18},
        'converged': {'role': 'standby', 'sync': 'tracking',
                      'field_claim': 'held', 'tick': 22},
        'promoted_probe': {'claim': 'held', 'owner': 424244,
                           'monitor': 'ctrl-b:8080'},
        'switch_durable': {
            'active': [{'seq': 4, 'tick': 14, 'event': {
                'field_claim_lost': {'point': 100, 'claimant': 424244}}},
                {'seq': 5, 'tick': 14, 'event': {'role_changed': {
                    'from': 'active', 'to': 'demoting',
                    'origin': 'fenced'}}},
                {'seq': 6, 'tick': 15, 'event': {'role_changed': {
                    'from': 'demoting', 'to': 'standby',
                    'origin': 'fenced'}}}],
            'standby': [{'seq': 3, 'tick': 12, 'event': {'role_changed': {
                'from': 'standby', 'to': 'promoting',
                'origin': 'request'}}}]},
        'demote': {'status': 200, 'report': {'role': 'demoting'}},
        'released': {'role': 'standby', 'sync': 'unsynchronized',
                     'field_claim': 'held', 'tick': 30},
        'release_probe': {'claim': 'held', 'owner': 424244,
                          'monitor': 'ctrl-b:8080'},
        'floor': {'active': 6, 'standby': 3},
        'served_floor': {'active': 6, 'standby': 3},
        'rows': [
            {'claim': 'held', 'owner': 424244, 'monitor': 'ctrl-b:8080',
             'demoted': {'role': 'standby', 'sync': 'unsynchronized',
                         'field_claim': 'held', 'tick': 31},
             'ex_owner': {'role': 'standby', 'sync': 'orphaned',
                          'field_claim': 'held', 'tick': 24}},
            {'claim': 'held', 'owner': 424244, 'monitor': 'ctrl-b:8080',
             'demoted': {'role': 'standby', 'sync': 'orphaned',
                         'field_claim': 'held', 'tick': 33},
             'ex_owner': {'role': 'standby', 'sync': 'orphaned',
                          'field_claim': 'held', 'tick': 26}},
            {'claim': 'held', 'owner': 424243, 'monitor': 'ctrl-a:8080',
             'demoted': {'role': 'standby', 'sync': 'orphaned',
                         'field_claim': 'held', 'tick': 35},
             'ex_owner': {'role': 'active', 'sync': None,
                          'field_claim': 'held', 'tick': 27}},
            {'claim': 'held', 'owner': 424243, 'monitor': 'ctrl-a:8080',
             'demoted': {'role': 'standby', 'sync': 'tracking',
                         'field_claim': 'held', 'tick': 40},
             'ex_owner': {'role': 'active', 'sync': None,
                          'field_claim': 'held', 'tick': 33}}],
        'answered': 4,
        'owners': [424243, 424244],
        'final': {'claim': 'held', 'owner': 424243,
                  'monitor': 'ctrl-a:8080',
                  'demoted': {'role': 'standby', 'sync': 'tracking',
                              'field_claim': 'held', 'tick': 40},
                  'ex_owner': {'role': 'active', 'sync': None,
                               'field_claim': 'held', 'tick': 33}},
        'durable': {
            'active': [{'seq': 7, 'tick': 26, 'event': {
                'field_claim_observed': {'point': 100,
                                         'claimant': 424244}}},
                {'seq': 8, 'tick': 26, 'event': {'role_changed': {
                    'from': 'standby', 'to': 'promoting',
                    'origin': 'reclaim'}}},
                {'seq': 9, 'tick': 27, 'event': {'role_changed': {
                    'from': 'promoting', 'to': 'active',
                    'origin': 'reclaim'}}}],
            'standby': [{'seq': 4, 'tick': 13, 'event': {'role_changed': {
                'from': 'active', 'to': 'demoting',
                'origin': 'request'}}},
                {'seq': 5, 'tick': 15, 'event': {'role_changed': {
                    'from': 'demoting', 'to': 'standby',
                    'origin': 'request'}}},
                {'seq': 6, 'tick': 32, 'event': {
                    'field_orphaned': {'aligned': 26}}}]},
        'served': {
            'active': [{'seq': 7, 'tick': 26, 'event': {'role_changed': {
                'from': 'standby', 'to': 'promoting',
                'origin': 'reclaim'}}}],
            'standby': [{'seq': 4, 'tick': 15, 'event': {'role_changed': {
                'from': 'demoting', 'to': 'standby',
                'origin': 'request'}}}]},
        'restored': True,
        'restored_owner': 'active',
        'restored_probe': {'claim': 'held', 'owner': 424243,
                           'monitor': 'ctrl-a:8080'}}


def scenario_demote_release_stays_released(ctx):
    """Exercise the voluntary-demote released-claim contract on the
    deployed pair: settle the pair on its launch layout, POST /promote
    the tracking peer so the launch owner fences and demotes in place,
    then POST /demote the new owner so its claim is deliberately
    handed back — and assert through both peers' serving monitors, a
    dedicated attachment's claim probes, and both durable journals
    that the released claim stays released: no re-arm under the
    demoted member's own token, any re-arm that does land journaled by
    name, the resolution attributable in the durable trail, the pair
    reconverging to one active plus one tracking standby with no
    operator re-promote and no restart, the reconverged owner's writes
    reaching the field, and the launch claim state and roles
    restored; two passes produce identical digests."""
    case = Case(
        'demote-release-stays-released',
        'A voluntary demote\'s released claim is not re-armed under '
        'the demoted member\'s own token',
        'with the deployed pair settled and tracking, POST /promote '
        'the tracking peer so the launch owner fences and begins '
        'tracking, then POST /demote the new owner: through both '
        'peers\' serving monitors, the lane\'s claim-aware attachment, '
        'and both durable journals the released claim stays released '
        '— the field probing unclaimed under the released token until '
        'a documented conditional path resolves it, the demoted '
        'member journaling no re-arm of its own hand-back, any re-arm '
        'that does land journaled by name, and the resolution named in '
        'the durable trail — while the fencing-loss-armed ex-owner\'s '
        'bound reclaim or the documented successor path takes the '
        'field, the pair reconverges to one active plus one tracking '
        'standby with no operator re-promote or restart, the '
        'reconverged owner\'s writes reach the field, and the '
        'launch claim state and roles restore; two passes produce '
        'identical digests')
    try:
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the run context carries '
                               'only one endpoint — the pair the '
                               'released-claim leg needs is absent')
        if not ctx.get('plant'):
            return case.finish('inconclusive', 'the run context carries '
                               'no plant endpoint — the claim-aware '
                               'attachment the leg\'s probe verdicts '
                               'ride is absent')
        if ctx.get('plant_ctl') is None:
            return case.finish('inconclusive', 'the run context carries '
                               'no shipped plant tool — the field '
                               'liveness witness the reconverged owner\'s '
                               'writes are read through is absent')
        tokens = ctx.get('plant_owner') or {}
        if not tokens.get('active') or not tokens.get('standby'):
            return case.finish('inconclusive', 'the run pins no '
                               'plant-writer owner tokens for the pair '
                               'endpoints — the released claim\'s '
                               'attribution cannot be read')
        journal_files = ctx.get('journal_files') or {}
        missing = [name for name in ('active', 'standby')
                   if journal_files.get(name) is None]
        if missing:
            return case.finish('inconclusive', 'the run context carries '
                               'no journal files for '
                               + json.dumps(missing) + ' — the durable '
                               'half of the released-claim audit is '
                               'absent')
        for name in ('active', 'standby'):
            try:
                _role(ctx, ctx[name])
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s monitor '
                                   'is unreachable: ' + str(exc)[:200])
        # The contract surface: a third-party mutation must already
        # fence on the standing owner's own pinned token — an open or
        # unattributed claim predates the field arbitration this leg's
        # probes read.
        probe = _try_plant(ctx, {'op': 'step', 'dt': 0})
        if not _fenced(probe):
            return case.finish('inconclusive', 'a third-party mutation '
                               'answered unfenced — the rig predates the '
                               'field claim the leg releases: '
                               + json.dumps(probe)[:300])
        if (probe or {}).get('error', {}).get('owner') is None:
            return case.finish('inconclusive', 'the fencing verdict names '
                               'no standing owner — the rig predates the '
                               'claim-attribution contract')
        # The lifecycle verb the leg's own claim probe needs: a build
        # answering invalid_request carries no probe_writer.
        probe_io = _plant_connect(ctx)
        try:
            observed = _plant_request(probe_io, {'op': 'probe_writer'})
        except Exception as exc:
            return case.finish('inconclusive', 'the claim-aware '
                               'attachment never got an answer: '
                               + str(exc)[:200])
        finally:
            try:
                probe_io.close()
            except Exception:
                pass
        observed = observed if isinstance(observed, dict) else {}
        if observed.get('error', {}).get('kind') not in (
                'fenced', 'unclaimed') and observed.get('result') != 'done':
            return case.finish('inconclusive', 'the field answered no '
                               'claim observation to a probe_writer — '
                               'the rig predates the released-claim '
                               'contract: ' + json.dumps(observed)[:300])
        owner = wait_for(lambda: _pair_active(ctx),
                         time.monotonic() + DR_SETTLE, interval=DR_POLL)
        if owner is None:
            return case.finish('failed', 'no peer reports role=active')
        peer = 'standby' if owner == 'active' else 'active'
        case.observe('field owner: ' + owner + ' (' + ctx[owner]
                     + ') under pinned token ' + hex(tokens[owner])
                     + '; tracking peer: ' + peer + ' (' + ctx[peer]
                     + ') under ' + hex(tokens[peer]))
        digests = []
        try:
            for number in (1, 2):
                violations = {}

                def note(key, diagnostic, detail):
                    violations.setdefault(key, (diagnostic, detail))

                record, evidence = _dr_pass(ctx, number, owner, peer,
                                            tokens, journal_files)
                evidence['record'] = record
                # A staged revision predating the contract presents the
                # pre-fix shape instead of a verdict the contract's own
                # judgement could read: the leg reports that
                # inconclusive rather than calling the silence a
                # failure of a rule the build never carried.
                pre_contract = _dr_pre_contract(record)
                if pre_contract:
                    evidence['pre_contract'] = pre_contract
                    ref = save_evidence(
                        ctx['evidence_dir'],
                        'demote-release-pass-' + str(number) + '.json',
                        evidence)
                    case.evidence('file', ref, 'demote-release pass '
                                  + str(number) + ' — the pre-contract '
                                  'shape the staged revision presented')
                    return case.finish('inconclusive', pre_contract)
                _dr_judge(record, note)
                digest = _dr_digest(record, violations)
                record['digest'] = dict(digest)
                record['violations'] = {
                    key: diagnostic
                    for key, (diagnostic, _) in violations.items()}
                evidence['digest'] = dict(digest)
                evidence['violations'] = record['violations']
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'demote-release-pass-' + str(number) + '.json',
                    evidence)
                case.evidence('file', ref, 'demote-release pass '
                              + str(number) + ' — the settled launch '
                              'layout, the promotion and the fenced '
                              'demotion it caused, the voluntary '
                              'demote and the release it left, the '
                              'hand-off window\'s claim-probe rows and '
                              'both peers\' serving roles, the durable '
                              'windows above the demote, the watched '
                              'field point\'s stamps, the restored '
                              'launch layout, and the normalized digest')
                if violations:
                    name = DIAG_FAILED if any(
                        diagnostic == DIAG_FAILED
                        for diagnostic, _ in violations.values()) \
                        else DIAG_NONDET
                    return case.finish(
                        'failed', name + ': ' + '; '.join(
                            detail for _, detail
                            in list(violations.values())[:4]))
                digests.append(digest)
        finally:
            # The launch claim state and roles for the legs behind
            # this one: a clean pass restores them by construction; an
            # aborted pass may have left the field's ownership on the
            # demoted peer, so the documented order runs again.
            if not _dr_restore(ctx, owner, peer,
                               time.monotonic() + DR_RESTORE):
                case.observe('cleanup: the pair did not settle back on '
                             'its launch claim state and roles')
        if len(digests) < 2:
            return case.finish('inconclusive', 'the released-claim '
                               'window never produced two clean passes')
        if digests[0] != digests[1]:
            return case.finish(
                'failed', DIAG_NONDET + ': the two passes\' digests '
                'diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two demote-release passes, identical digests: '
                     + json.dumps(digests[0], sort_keys=True))

        # The unchecked-diagnostic self-check: the judge replays each
        # planted negative it must name; a silent judge means the leg
        # can no longer catch what it names.
        slipped = _dr_self_check()
        if slipped:
            return case.finish('failed', DIAG_UNCHECKED
                               + ': planted negatives slipped the '
                               'leg’s own audits: '
                               + ', '.join(slipped))
        case.observe('the self-check leg’s planted negatives each '
                     'reported their named diagnostic')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive',
                           'the leg could not complete on this rig: '
                           + str(exc)[:500])


def _dr_pre_contract(record):
    """The pre-#1270 signature, read off one pass's record: the
    released claim never left the demoted member's token across the
    whole window, no peer journaled a re-arm naming it (the durable
    record could not show who re-took the claim), and the only member
    that ever stood on the field was the demoted one re-taking the
    claim it had handed back — the shape a build whose orphan-cycle
    ensure still runs against a voluntary demotion's release presents,
    and one the contract's own `yielded` suppression makes impossible.
    A re-arm journaled by name under that token is the contract's own
    failure, not this signature, and reads as such."""
    if record.get('stage_error') is not None:
        return None
    rows = record.get('rows') or []
    tokens = record.get('tokens') or {}
    owner, peer = record.get('owner'), record.get('peer')
    if not rows or record.get('answered') is None:
        return None
    answered = [row for row in rows
                if row.get('claim') in ('held', 'unclaimed')]
    if not answered:
        return None
    if any(row.get('claim') != 'held'
           or row.get('owner') != tokens.get(peer) for row in answered):
        return None
    durable = record.get('durable') or {}
    # An unread durable journal cannot show a re-arm the record does
    # carry: the signature needs the whole trail readable.
    for name in (owner, peer):
        if durable.get(name) is None:
            return None
        if _dr_events(durable[name], 'field_claim_rearmed'):
            return None
    walk = _dr_walk(durable.get(peer)) or []
    if not any(row[1] in ('promoting', 'active') for row in walk):
        return None
    return ('the demoted member\'s own orphan machinery re-armed the '
            'released claim under its own token and the claim never left '
            'it — nothing named the re-arm, and the only member that '
            'stood on the field was the demoted one re-taking the claim '
            'it handed back: a staged revision predating the '
            'voluntary-demote released-claim contract')
