"""The peer_announce acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *


# --------------------------------------------------------------------
# The checkpoint `?peer=` announce acceptance contract — the settled
# #645 behavior pinned as per-run lane evidence for WW-LCM-001's
# takeover-continuity clause: the monitor records a tracking peer's
# `GET /checkpoint?peer=` announce only when it names the pulling
# connection's own source address, so a foreign client cannot rewrite
# the announced tracking source a demoted peer later follows. Before
# the fix any client's pull carrying a crafted `?peer=` silently
# overwrote the announced fallback — a launched active demoted
# mid-run then followed the poisoned address and stranded
# unsynchronized and unpromotable instead of tracking its real
# successor. The case proves the acceptance rule on the deployed
# pair: with the pair settled — the standby's genuine announce
# already recorded on the field owner's monitor through its own
# checkpoint pulls — a foreign `GET /checkpoint?peer=<dead address>`
# from a third client still answers the checkpoint document while
# the crafted announce is refused; the documented demote-then-promote
# switch then leaves the demoted peer following the genuine announced
# successor — standby reconverging to tracking rather than stranding
# unsynchronized — while the promoted peer holds the plant's writer
# claim and the demoted peer's field writes fence; the pair finally
# restores its launch roles. Named diagnostics peer-announce-failed
# for a serving-contract miss and peer-announce-nondeterministic when
# two passes disagree.

PEER_ANNOUNCE_SETTLE = 45  # bound on each demote/promote and the
                           # pair's reconvergence
PEER_ANNOUNCE_POLL = 0.4   # wait cadence inside the leg
# The crafted `?peer=` value — the #645 reproduction's bogus address:
# a foreign IP no pulling connection owns, unroutable from the rig,
# so a landed announce strands the demotion's tracking pull on a dead
# address while a refused one changes nothing.
PEER_ANNOUNCE_DEAD = '10.255.255.1:9999'


def _peer_announce_pass(ctx, number, entry_owner, point, baseline):
    """One announce-acceptance pass: the foreign announce against the
    settled field owner, the documented demote-then-promote switch,
    and the restore to the entry roles. Returns (digest, violations,
    evidence): digest is the pass's normalized verdict record,
    identical across clean passes; violations is {key: (diagnostic,
    detail)} in first-seen order."""
    violations = {}
    evidence = {'entry_owner': entry_owner, 'pass': number}
    digest = {'announce': 'refused', 'demoted': 'unsynchronized',
              'claim': 'unanswered', 'command': 'unanswered',
              'roles': 'switched'}

    def note(key, diagnostic, detail):
        violations.setdefault(key, (diagnostic, detail))

    def failed(key, detail):
        note(key, 'peer-announce-failed', detail)

    owner = entry_owner
    peer = 'standby' if owner == 'active' else 'active'
    base, peer_base = ctx[owner], ctx[peer]
    evidence.update({'owner': owner, 'peer': peer})

    # The settled gate: the entry owner holds the field and the other
    # launched peer tracks it — the posture whose checkpoint pulls
    # recorded the genuine announce this leg must not lose.
    if _pair_active(ctx) != owner \
            or _tracking_standby(ctx, peer) is None:
        failed('settle', 'the pair never settled — ' + owner
               + ' holds no active role with ' + peer
               + ' tracking behind it')
        return None, violations, evidence

    # The baseline checkpoint the crafted read must still answer.
    try:
        _, checkpoint = http_json('GET', base + '/checkpoint')
    except Exception as exc:
        failed('checkpoint-baseline', 'the field owner\'s checkpoint '
               'never answered: ' + str(exc)[:200])
        return None, violations, evidence
    if not isinstance(checkpoint, dict) \
            or not isinstance(checkpoint.get('tick'), int) \
            or checkpoint.get('model_fingerprint') is None:
        failed('checkpoint-baseline', 'the field owner serves no '
               'checkpoint document: '
               + json.dumps(checkpoint)[:200])
        return None, violations, evidence
    evidence['checkpoint'] = {
        'tick': checkpoint['tick'],
        'model_fingerprint': checkpoint['model_fingerprint']}

    # The crafted announce: a third client's `GET
    # /checkpoint?peer=<dead address>` against the field owner's
    # monitor. The read still answers the owner's checkpoint — a
    # refused announce is ignored, never an error — while the
    # announced tracking source must keep naming the genuine
    # successor, which only the demotion below can observe.
    try:
        announce_status, answered = http_json(
            'GET', base + '/checkpoint?peer=' + PEER_ANNOUNCE_DEAD)
    except urllib.error.HTTPError as exc:
        announce_status, answered = exc.code, None
        try:
            exc.close()
        except Exception:
            pass
    except Exception as exc:
        failed('crafted-read', 'the foreign GET /checkpoint?peer= '
               'never answered: ' + str(exc)[:200])
        return None, violations, evidence
    evidence['announce'] = {'peer': PEER_ANNOUNCE_DEAD,
                            'status': announce_status,
                            'checkpoint': answered}
    if announce_status == 200 and isinstance(answered, dict) \
            and answered.get('model_fingerprint') \
            == checkpoint['model_fingerprint'] \
            and isinstance(answered.get('tick'), int):
        digest['announce'] = 'answered'
    else:
        failed('crafted-read', 'the foreign GET /checkpoint?peer=<dead '
               'address> did not answer the checkpoint document: '
               + str(announce_status) + ' '
               + json.dumps(answered)[:200])

    # The documented switch: demote the field owner. A demotion with
    # no tracking source at all is refused up front — the announce
    # this leg guards is the fallback it follows.
    demote_status, demote = _settle_call(base + '/demote')
    evidence['demote'] = {'status': demote_status, 'body': demote}
    if demote_status != 200:
        failed('demote', 'the field owner\'s demote answered '
               + str(demote_status) + ': ' + json.dumps(demote)[:300])
        return None, violations, evidence

    # The acceptance proof: the demoted peer reconverges tracking
    # behind the genuine announced successor. A landed crafted
    # announce strands it here — unsynchronized against the dead
    # address, unpromotable — instead.
    deadline = time.monotonic() + PEER_ANNOUNCE_SETTLE
    reconverged = wait_for(lambda: _tracking_standby(ctx, owner),
                           deadline, interval=PEER_ANNOUNCE_POLL)
    report = _try_role(ctx, base)
    evidence['reconverged'] = report
    if reconverged is None:
        failed('reconverge', 'the demoted peer never reconverged '
               'tracking behind its announced successor — still '
               + json.dumps((report or {}).get('sync'))[:200]
               + ': the crafted announce rewrote the tracking source'
               ' it follows')
    else:
        digest['demoted'] = 'tracking'

    # The promotion half: the converged peer takes the field.
    promoted, last = None, None
    deadline = time.monotonic() + PEER_ANNOUNCE_SETTLE
    while time.monotonic() < deadline and promoted is None:
        promote_status, body = _settle_call(peer_base + '/promote')
        if promote_status == 200:
            promoted = body
        else:
            last = (promote_status, body)
            time.sleep(PEER_ANNOUNCE_POLL)
    evidence['promote'] = {'body': promoted, 'last_refusal': last}
    if promoted is None:
        failed('promote', 'the converged peer\'s promote never '
               'succeeded: ' + json.dumps(last)[:300])

    # The writer-claim half: the promoted peer scans on — its
    # telemetry advancing — while the plant's single-writer claim
    # fences a third attachment's mutation probe.
    snap0 = _try_snapshot(ctx, peer_base) or {}
    tick0 = snap0.get('tick')
    grown = wait_for(
        lambda: (snap.get('tick', 0) > tick0 and snap or None)
        if isinstance(tick0, int)
        and isinstance((snap := _try_snapshot(ctx, peer_base)), dict)
        else None,
        time.monotonic() + PEER_ANNOUNCE_SETTLE,
        interval=PEER_ANNOUNCE_POLL)
    probe = _try_plant(ctx, {'op': 'step', 'dt': 0})
    evidence['claim'] = {'tick': tick0,
                         'advanced': (grown or {}).get('tick'),
                         'probe': probe}
    if grown is None:
        failed('telemetry', 'the promoted peer\'s telemetry never '
               'advanced — the field writer did not take over')
    if probe is None:
        failed('fencing', 'the plant never answered the writer-claim '
               'probe')
    elif not _fenced(probe):
        failed('fencing', 'the promoted peer holds no enforceable '
               'writer claim — a third attachment\'s probe answered '
               + json.dumps(probe)[:300])
    else:
        digest['claim'] = 'fenced'

    # The fenced-demoted half: a receipted write aimed at the demoted
    # peer answers the named not_active refusal instead of reaching
    # the field.
    command = {'command': {'write_value': {
        'point': point, 'kind': 'bool',
        'value': {'bool': not baseline}}},
        'actor': 'qa-lane-peer-announce-' + str(number)}
    try:
        command_status, receipt = http_json(
            'POST', base + '/command', command)
    except Exception as exc:
        failed('command', 'the demoted peer never answered the '
               'fencing write: ' + str(exc)[:200])
    else:
        outcome = _outcome_key(receipt)
        evidence['command'] = {'status': command_status,
                               'outcome': outcome}
        if command_status == 200 and outcome == 'rejected:not_active':
            digest['command'] = 'refused'
        else:
            failed('command', 'the demoted peer admitted a field '
                   'write instead of answering the named not_active '
                   'refusal: ' + str(command_status) + ' '
                   + json.dumps(receipt)[:300])

    # The restore: the documented order back to the entry roles, so
    # the next pass — and the cases behind this one — meet the launch
    # layout again.
    restore_status, restore_body = _settle_call(
        peer_base + '/demote')
    evidence['restore_demote'] = {'status': restore_status,
                                  'body': restore_body}
    if restore_status != 200:
        failed('restore-demote', 'the restore demote answered '
               + str(restore_status) + ': '
               + json.dumps(restore_body)[:300])
    else:
        repromoted, last_restore = None, None
        deadline = time.monotonic() + PEER_ANNOUNCE_SETTLE
        while time.monotonic() < deadline and repromoted is None:
            promote_status, body = _settle_call(base + '/promote')
            if promote_status == 200:
                repromoted = body
            else:
                last_restore = (promote_status, body)
                time.sleep(PEER_ANNOUNCE_POLL)
        evidence['restore_promote'] = {'body': repromoted,
                                       'last_refusal': last_restore}
        if repromoted is None:
            failed('restore-promote', 'the entry owner\'s restore '
                   'promote never succeeded: '
                   + json.dumps(last_restore)[:300])
    settled = wait_for(
        lambda: (_pair_active(ctx) == owner or None)
        and _tracking_standby(ctx, peer),
        time.monotonic() + PEER_ANNOUNCE_SETTLE,
        interval=PEER_ANNOUNCE_POLL)
    evidence['restored'] = settled
    if settled is None:
        failed('restore-settle', 'the pair never settled back to its '
               'entry role layout')
    else:
        digest['roles'] = 'restored'
    evidence['digest'] = dict(digest)
    evidence['violations'] = {key: diagnostic
                              for key, (diagnostic, _)
                              in violations.items()}
    return digest, violations, evidence


def scenario_peer_announce(ctx):
    """Exercise the checkpoint ?peer= announce acceptance contract on
    the deployed pair: a foreign announce is refused while the read
    still answers, and the demoted peer tracks its genuine successor."""
    case = Case(
        'peer-announce',
        'Checkpoint ?peer= announce acceptance on the deployed pair',
        'with the deployed pair settled — the standby\'s genuine '
        'announce already recorded on the field owner\'s monitor '
        'through its own checkpoint pulls — a foreign GET '
        '/checkpoint?peer=<dead address> from a third client still '
        'answers the checkpoint document while the crafted announce '
        'is refused; the documented demote-then-promote switch then '
        'leaves the demoted peer following the genuine announced '
        'successor — standby reconverging to tracking rather than '
        'stranding unsynchronized — while the promoted peer holds the '
        'plant\'s writer claim and the demoted peer\'s field writes '
        'fence; the pair finally restores its launch roles, and two '
        'passes produce identical digests')
    try:
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries only one endpoint — the pair '
                               'the announce leg needs is absent')
        if ctx.get('plant') is None:
            return case.finish('inconclusive', 'the run publishes no '
                               'plant endpoint — the writer-claim leg '
                               'cannot run')
        for name in ('active', 'standby'):
            try:
                _role(ctx, ctx[name])
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s '
                                   'monitor is unreachable: '
                                   + str(exc)[:200])
        deadline = time.monotonic() + PEER_ANNOUNCE_SETTLE
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=PEER_ANNOUNCE_POLL)
        if owner is None:
            return case.finish('failed', 'no peer reports role=active')
        peer = 'standby' if owner == 'active' else 'active'
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=PEER_ANNOUNCE_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the genuine '
                               'announce the leg guards was never '
                               'recorded')
        case.observe('field owner: ' + owner + ' (' + ctx[owner]
                     + '); tracking successor: ' + peer)
        _, signals = http_json('GET', ctx[owner] + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'peer-announce-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming the fenced '
                      'write\'s target point')
        target = _writable_bool_point(signals)
        if target is None or target.get('point') is None:
            return case.finish('inconclusive', 'the model declares '
                               'no writable bool in-point for the '
                               'fenced write')
        point = target['point']
        baseline = _point_value(_snapshot(ctx, ctx[owner]), point)
        if not isinstance(baseline, bool):
            return case.finish('inconclusive', 'point ' + str(point)
                               + ' serves no bool baseline to write '
                               'against')
        digests = []
        try:
            for number in (1, 2):
                digest, violations, evidence = _peer_announce_pass(
                    ctx, number, owner, point, baseline)
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'peer-announce-pass-' + str(number) + '.json',
                    evidence)
                case.evidence('file', ref, 'announce-acceptance pass '
                              + str(number) + ' — the crafted read, '
                              'the switch answers, the reconvergence '
                              'and fencing legs, and the normalized '
                              'digest')
                if violations or digest is None:
                    diagnostic = 'peer-announce-failed' \
                        if digest is None or any(name
                               == 'peer-announce-failed'
                               for name, _ in violations.values()) \
                        else \
                        'peer-announce-nondeterministic'
                    return case.finish(
                        'failed', diagnostic + ': ' + '; '.join(
                            detail for _, detail in
                            list(violations.values())[:4]))
                digests.append(digest)
        finally:
            # The pair's entry layout for the cases behind this one —
            # a clean pass restores it by construction; an aborted
            # pass gets the documented order run again, best-effort.
            current = _pair_active(ctx)
            other = 'standby' if owner == 'active' else 'active'
            if current != owner \
                    and _tracking_standby(ctx, owner) is not None:
                try:
                    if current is not None:
                        _settle_call(ctx[current] + '/demote')
                    _settle_call(ctx[owner] + '/promote')
                    wait_for(
                        lambda: (_pair_active(ctx) == owner or None)
                        and _tracking_standby(ctx, other),
                        time.monotonic() + PEER_ANNOUNCE_SETTLE,
                        interval=PEER_ANNOUNCE_POLL)
                    case.observe('cleanup: restored the entry role '
                                 'layout')
                except Exception as exc:
                    case.observe('cleanup: role restore failed: '
                                 + str(exc)[:200])
        if digests[0] != digests[1]:
            return case.finish(
                'failed', 'peer-announce-nondeterministic: '
                'the two passes\' digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two announce-acceptance passes, identical '
                     'digests')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
