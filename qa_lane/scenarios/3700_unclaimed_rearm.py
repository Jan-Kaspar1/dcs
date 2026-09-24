"""The unclaimed_rearm acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: The unclaimed-rearm case is the same shape: its preempt-and-release
# induction opens the ownerless window behind whichever peer owns the
# field, watches the recorded owner's inline re-arm and the fencing
# it restores, and leaves the claim state and launch roles as found.


# --------------------------------------------------------------------
# The unclaimed-field inline re-arm contract (WW-LCM-001's continuity
# clause, WW-OPS-003's field-confidence clause, the settled #621
# behavior the qax-20260918-008 run verified on this rig): a field
# write refused `unclaimed` while no claim stands is a recoverable
# ownerless window, not supersession — the recorded owner re-arms
# inline through one conditional, non-preempting `ensure_writer` and
# the write lands without field_claim_lost or demotion, while a write
# refused under another standing claim still fences and demotes. The
# leg opens that window deliberately: a dedicated attachment's
# `claim_writer` preempts the standing claim and its `release_writer`
# hands it back in the same breath, so the field sits unclaimed
# behind the owner's live connection — then the watch proves the
# owner's next scan re-armed in place (its writes keep landing, its
# role and journals unmoved, the fencing-loss ledger empty) and that
# the re-armed claim is real: a foreign attachment's conditional
# ensure and write probes stay fenced. The claim ops stay on the raw
# client — `claim_writer` is an op `dcs-plant-ctl` does not expose,
# and the tool's conditional claim could never preempt the owner into
# the window — while the census and field reads ride the shipped
# tool. The named diagnostics are unclaimed-rearm-failed and
# unclaimed-rearm-nondeterministic; two consecutive passes must
# produce identical digests.

UNCLAIMED_REARM_SETTLE = 30    # bound on the pair reporting settled
UNCLAIMED_REARM_POLL = 0.4     # cadence watching the owner mid-window
UNCLAIMED_REARM_ROUNDS = 6     # polls through the post-window watch
UNCLAIMED_REARM_DEADLINE = 15  # bound on the anchor and landing waits
# The dedicated attachment's foreign owner token — never a
# controller's pinned token nor the tool's "dcs-pltc": the preempting
# claim that opens the ownerless window behind the standing owner's
# live connection.
UNCLAIMED_REARM_FOREIGN = 0x7161_2d72_6561_726d  # "qa-rearm"


def _write_fenced(response):
    """Whether a write probe's answer is the point-level fencing
    verdict — `{"kind":"io","error":{"fenced":N}}` — the refusal the
    standing claim gives a non-holder's write (the `step` probe's
    named `fenced` kind has no point-level counterpart)."""
    error = (response or {}).get('error') or {}
    inner = error.get('error')
    return error.get('kind') == 'io' and isinstance(inner, dict) \
        and 'fenced' in inner


def _unclaimed_rearm_pass(ctx, active, peer, watch, probe_point):
    """One induction pass: the dedicated attachment's preempt-and-
    release opens the ownerless window behind the field owner's live
    connection, then the watch proves the recorded owner re-armed
    inline — its write landing, its role and journals unmoved — and
    the foreign probes prove the re-armed claim is real. Returns
    (digest, violations, evidence): digest is the pass's normalized
    verdict record, identical across clean passes; violations is
    {key: (diagnostic, detail)} in first-seen order."""
    base, peer_base = ctx[active], ctx[peer]
    violations = {}
    evidence = {'watch_point': watch, 'probe_point': probe_point}

    def note(key, diagnostic, detail):
        violations.setdefault(key, (diagnostic, detail))

    def failed(key, detail):
        note(key, 'unclaimed-rearm-failed', detail)

    # The audit positions the window must leave untouched: each peer's
    # journal floor, the fencing-loss ledger the owner's io_health
    # keeps, the watched field output's stamp, and the standing
    # claim's fencing answer.
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
        failed('baseline-claim', 'the field held no standing writer '
               'claim at pass start — a third attachment\'s mutation '
               'probe answered ' + json.dumps(probe0)[:300])

    # The induction: claim_writer preempts unconditionally, the
    # same-breath release_writer empties the holder set — the field
    # sits unclaimed while the recorded owner's connection stays
    # live, the #621 reproduction's window.
    stream = _plant_connect(ctx)
    released = False
    try:
        claim = _plant_request(stream, {'op': 'claim_writer',
                                        'owner': UNCLAIMED_REARM_FOREIGN})
        evidence['claim'] = claim
        verdict = claim.get('result')
        if verdict == 'claimed_shared':
            note('claim-shared', 'unclaimed-rearm-nondeterministic',
                 'the preempting claim joined a live foreign holder '
                 '— a leaked attachment shares the induction token: '
                 + json.dumps(claim)[:200])
        elif verdict != 'done':
            raise ConnectionError('the preempting claim was refused: '
                                  + json.dumps(claim)[:300])
        release = _plant_request(stream, {'op': 'release_writer'})
        evidence['release'] = release
        released = release.get('result') == 'done'
        if not released:
            raise ConnectionError('the claim hand-back was refused: '
                                  + json.dumps(release)[:300])

        # The window observed from the released attachment itself:
        # `unclaimed` while the ownerless window still stands,
        # `fenced` once the owner's re-arm already landed — both the
        # fail-closed field; only a mutation answer is the defect.
        window = _plant_request(stream, {'op': 'step', 'dt': 0})
        evidence['window_probe'] = window
        kind = _probe_error(window)
        if kind not in ('unclaimed', 'fenced'):
            failed('window-open', 'the field accepted a third-party '
                   'mutation with no claim standing: '
                   + json.dumps(window)[:300])

        # The landing anchor: the watched output's stamp at release
        # time — the first post-window write must stamp past it, and
        # only the recorded owner's re-arm can land one.
        anchor = wait_for(lambda: _probe_sample(ctx, watch),
                          time.monotonic() + UNCLAIMED_REARM_DEADLINE,
                          interval=UNCLAIMED_REARM_POLL)
        if not isinstance(anchor, dict) \
                or not isinstance(anchor.get('tick'), int):
            raise ConnectionError('the field read anchoring the '
                                  'landing check never answered a '
                                  'stamped sample')
        anchor = anchor['tick']

        # The watch: the owner re-arms and its write lands, the pair's
        # roles hold, the ledger stays empty, and third-party probes
        # meet the fail-closed field throughout.
        rounds = []
        landed = role_moved = ledger_grew = False
        answered = 0
        last_field = anchor
        for index in range(UNCLAIMED_REARM_ROUNDS):
            report = _try_role(ctx, base)
            partner = _try_role(ctx, peer_base)
            snap = _try_snapshot(ctx, base)
            sample = _probe_sample(ctx, watch)
            probe = _try_plant(ctx, {'op': 'step', 'dt': 0})
            health = (snap or {}).get('io_health') or {}
            ftick = (sample or {}).get('tick')
            rounds.append({'owner': (report or {}).get('role'),
                           'peer': (partner or {}).get('role'),
                           'tick': (snap or {}).get('tick'),
                           'field_tick': ftick,
                           'failed_writes':
                               health.get('failed_writes'),
                           'probe': _probe_error(probe)})
            if report is None and partner is None and snap is None:
                if index + 1 < UNCLAIMED_REARM_ROUNDS:
                    time.sleep(UNCLAIMED_REARM_POLL)
                continue  # a dropped poll — one lost observation
            answered += 1
            if report is not None and report.get('role') != 'active':
                failed('owner-moved', 'the field owner left '
                       'role=active through the unclaimed window — '
                       'the refused write demoted it instead of '
                       're-arming inline: '
                       + json.dumps(report)[:300])
                role_moved = True
                break
            if partner is not None \
                    and partner.get('role') != 'standby':
                failed('peer-moved', 'the tracking peer reported a '
                       'spurious role change through the window: '
                       + json.dumps(partner)[:300])
                role_moved = True
                break
            if (health.get('failed_writes') or 0) > ledger0:
                ledger_grew = True
                failed('ledger-grew', 'the fencing-loss ledger grew '
                       'through the window — io_health.failed_writes '
                       + str(health.get('failed_writes'))
                       + ' over the baseline ' + str(ledger0))
            if probe is not None and not _fenced(probe) \
                    and _probe_error(probe) != 'unclaimed':
                failed('probe-wrote', 'a third-party mutation probe '
                       'wrote through during the watch: '
                       + json.dumps(probe)[:300])
            if isinstance(ftick, int) and isinstance(anchor, int) \
                    and ftick > anchor:
                landed = True
                last_field = ftick
            if index + 1 < UNCLAIMED_REARM_ROUNDS:
                time.sleep(UNCLAIMED_REARM_POLL)
        evidence['watch'] = rounds
        if answered == 0:
            raise ConnectionError('the pair\'s monitors never '
                                  'answered through the watch')
        if not landed:
            failed('write-stalled', 'the owner\'s write never landed '
                   'through the unclaimed window — the inline re-arm '
                   'dropped it or never ran (field tick held at '
                   + str(anchor) + ')')

        # The journal audit: no field_claim_lost and no role
        # transition above either peer's floor — the fencing-loss
        # ledger stays empty per the settled contract.
        journal_clean = True
        for name in (active, peer):
            try:
                _, journal = http_json(
                    'GET', ctx[name] + '/journal?since='
                    + str(floors[name]))
            except Exception as exc:
                journal_clean = False
                failed('journal-' + name, name + '\'s journal never '
                       'served the post-window audit: '
                       + str(exc)[:200])
                continue
            for entry in _journal_list(journal):
                event = entry.get('event') or {}
                if 'field_claim_lost' in event:
                    journal_clean = False
                    failed('claim-journal-' + name, name + ' journaled '
                           'field_claim_lost through the window — the '
                           'owner fenced instead of re-arming')
                if 'role_changed' in event:
                    journal_clean = False
                    failed('role-journal-' + name, name + ' journaled '
                           'a role transition through the window')

        # The re-arm produced a real claim: a fresh probe fences, the
        # foreign attachment's conditional ensure is refused, and its
        # write probe stays fenced — while the owner's writes keep
        # landing after them.
        fence = _try_plant(ctx, {'op': 'step', 'dt': 0})
        evidence['rearm_probe'] = fence
        if fence is None:
            raise ConnectionError('the plant never answered the '
                                  'post-window fencing probe')
        if _probe_error(fence) == 'unclaimed':
            failed('never-rearmed', 'the field stayed unclaimed — '
                   'the recorded owner never re-armed its claim')
        elif not _fenced(fence):
            failed('rearm-open', 'the field answered a third-party '
                   'probe without the re-armed claim\'s fencing: '
                   + json.dumps(fence)[:300])
        ensure = _plant_request(stream, {'op': 'ensure_writer',
                                         'owner': UNCLAIMED_REARM_FOREIGN})
        evidence['foreign_ensure'] = ensure
        if not _fenced(ensure):
            failed('foreign-ensure', 'a foreign attachment\'s '
                   'conditional claim was admitted past the re-armed '
                   'claim — the re-arm produced no real claim: '
                   + json.dumps(ensure)[:300])
        probe_sample = _probe_sample(ctx, probe_point)
        if not isinstance(probe_sample, dict):
            raise ConnectionError('the field read sizing the foreign '
                                  'write probe never answered')
        value = probe_sample.get('value') or {'bool': True}
        write = _plant_request(stream, {'op': 'write',
                                        'point': probe_point,
                                        'value': value})
        evidence['foreign_write'] = write
        if not _write_fenced(write):
            failed('foreign-write', 'a foreign attachment\'s write '
                   'probe was not fenced under the re-armed claim: '
                   + json.dumps(write)[:300])

        def advancing():
            _try_snapshot(ctx, base)  # the owner's scans keep running
            sample_ = _probe_sample(ctx, watch)
            if not isinstance(sample_, dict):
                return None
            return sample_ if isinstance(sample_.get('tick'), int) \
                and sample_['tick'] > last_field else None
        sustained = wait_for(advancing,
                             time.monotonic() + UNCLAIMED_REARM_DEADLINE,
                             interval=UNCLAIMED_REARM_POLL)
        evidence['sustained'] = sustained
        if sustained is None:
            failed('writes-stalled', 'the owner\'s writes stopped '
                   'landing after the foreign probes — the re-armed '
                   'claim fences its own owner')
    finally:
        stream.close()
        if not released:
            # Best effort: a stranded foreign claim would fence the
            # owner's re-arm — re-open the ownerless window through a
            # fresh attachment so the recorded owner can reclaim.
            try:
                restore = _plant_connect(ctx)
                try:
                    _plant_request(restore, {'op': 'claim_writer',
                                             'owner':
                                             UNCLAIMED_REARM_FOREIGN})
                    _plant_request(restore, {'op': 'release_writer'})
                finally:
                    restore.close()
            except Exception:
                pass  # an unreachable plant is the pass's own verdict
        # Best effort: the launch role layout for the legs behind this
        # one — a rig that demoted the owner or moved the peer gets
        # the settled active/standby pair back; a clean pass moves
        # nothing, so neither restore fires.
        if (_try_role(ctx, base) or {}).get('role') != 'active':
            try:
                http_json('POST', base + '/promote')
            except Exception:
                pass
        if (_try_role(ctx, peer_base) or {}).get('role') != 'standby':
            try:
                http_json('POST', peer_base + '/demote')
            except Exception:
                pass

    evidence['digest'] = {
        'baseline': 'fenced' if _fenced(probe0) else 'unfenced',
        'claim': 'shared' if verdict == 'claimed_shared'
                 else 'granted' if verdict == 'done' else 'refused',
        'window': 'closed' if kind in ('unclaimed', 'fenced')
                  else 'open',
        'writes': 'landed' if landed else 'stalled',
        'roles': 'unchanged' if not role_moved else 'moved',
        'ledger': 'empty' if not ledger_grew else 'grew',
        'journal': 'clean' if journal_clean else 'transitioned',
        'fence': ('fenced' if _fenced(fence)
                  else 'unclaimed'
                  if _probe_error(fence) == 'unclaimed' else 'open'),
        'foreign': 'fenced' if _fenced(ensure) else 'admitted',
        'foreign-write': 'fenced' if _write_fenced(write)
                         else 'landed',
        'sustained': 'landing' if sustained is not None else 'stalled'}
    return evidence['digest'], violations, evidence


def scenario_unclaimed_rearm(ctx):
    """Open the unclaimed window behind the field owner's live
    connection — a dedicated attachment's preempt-and-release — and
    prove the recorded owner's next scan re-arms inline: the write
    lands, the owner stays active, nothing journals, the ledger
    stays empty, and the re-armed claim fences foreign probes."""
    case = Case('unclaimed-rearm',
                'The unclaimed field re-arms the owner inline',
                'with the deployed pair settled and the field-owning '
                'peer holding the plant\'s writer claim, a dedicated '
                'attachment\'s claim_writer/release_writer opens the '
                'ownerless window behind the owner\'s live '
                'connection — the #621 reproduction — and the '
                'owner\'s next scan re-arms inline through one '
                'conditional ensure_writer: its field write keeps '
                'landing, it reports active throughout, no '
                'field_claim_lost or role transition journals, and '
                'the fencing-loss ledger stays empty; the re-armed '
                'claim then fences a foreign attachment\'s '
                'ensure_writer and write probes while the owner\'s '
                'writes keep landing, and the rig returns to its '
                'launch claim state and roles; two consecutive '
                'passes produce identical digests')
    try:
        if not ctx.get('plant') or ctx.get('plant_ctl') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no plant endpoint or '
                               'plant_ctl seam — the claim ops and '
                               'field reads cannot run')
        deadline = time.monotonic() + UNCLAIMED_REARM_SETTLE
        active = wait_for(lambda: _settled_active(ctx), deadline)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        peer = 'standby' if active == 'active' else 'active'
        report = wait_for(lambda: _try_role(ctx, ctx[peer]), deadline)
        if report is None:
            return case.finish('inconclusive', 'the pair\'s other '
                               'endpoint never answered /role — the '
                               'peer-stability half cannot run')
        if report.get('role') != 'standby':
            return case.finish('inconclusive', 'the pair never '
                               'settled — ' + peer + ' reports '
                               + str(report.get('role')))
        case.observe('field owner: ' + active + ' (' + ctx[active]
                     + '); watching peer ' + peer)

        field_out = _field_out_points(ctx)
        if not field_out:
            return case.finish('inconclusive', 'the simulated plant '
                               'serves no field output to watch the '
                               'owner\'s writes on')
        watch = min(field_out)
        field_in = sorted(_field_inputs(ctx))
        probe_point = field_in[0] if field_in else watch
        case.observe('watching field out point ' + str(watch)
                     + '; foreign write probe on point '
                     + str(probe_point))

        digests = []
        for number in (1, 2):
            digest, violations, evidence = _unclaimed_rearm_pass(
                ctx, active, peer, watch, probe_point)
            ref = save_evidence(ctx['evidence_dir'],
                                'unclaimed-rearm-pass-'
                                + str(number) + '.json', evidence)
            case.evidence('file', ref, 're-arm pass ' + str(number)
                          + ' — the induction, the watch rounds, the '
                          'fencing legs, and the normalized digest')
            if violations:
                failed = any(name == 'unclaimed-rearm-failed'
                             for name, _ in violations.values())
                diagnostic = 'unclaimed-rearm-failed' if failed \
                    else 'unclaimed-rearm-nondeterministic'
                return case.finish(
                    'failed', diagnostic + ': ' + '; '.join(
                        detail for _, detail in
                        list(violations.values())[:4]))
            digests.append(digest)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', 'unclaimed-rearm-nondeterministic: the two '
                'passes\' digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two unclaimed-window passes, identical digests')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
