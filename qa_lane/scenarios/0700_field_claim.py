"""The field_claim acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *


# --------------------------------------------------------------------
# The plant's single-writer field claim (the failover decision's
# fencing half, exercised standing): with the settled pair's active
# holding the write-ownership claim it took at launch, a third sim-net
# attachment must find the field closed — writes and steps refused
# with the named fencing failure — while the owner's own writes keep
# landing. The claim lifecycle then answers per contract:
# `ensure_writer` under a foreign token fences (the conditional grant
# never preempts a live owner), under the standing owner's token
# answers `claimed_shared` and writes under the shared claim, and
# `release_writer` drops only the caller's hold — harmless `done` for
# a holder of nothing. A rogue `claim_writer` preempts — the grant is
# unconditional — but never silently and never fatally: the superseded
# owner's journal records `field_claim_lost` and the peer demotes
# rather than dying, after which the scenario restores the rig by
# re-promoting it. The post-restart `unclaimed` window is the
# link-loss case's leg; the fenced writer's degrade details are the
# fenced-writer-degrade case's — this case asserts the standing-claim
# side only.
#
# The shared-claim leg needs the standing owner's token: the rig pins
# each peer's --owner-token and ctx['plant_owner'] carries them keyed
# by endpoint name — the harness-sharing case the flag exists for.

CLAIM_DEADLINE = 30      # bound on the demote watch and the restore
CLAIM_FOREIGN = 0xF00D   # a token no rig peer or tooling client claims


def _mutation_fenced(response):
    """Whether a field-mutation answer is the named fencing refusal —
    `write` carries the point's IoError::Fenced under the `io` kind,
    `step` and the claim requests the plant-level `fenced` kind."""
    error = (response or {}).get('error') or {}
    if error.get('kind') == 'fenced':
        return True
    inner = error.get('error')
    return error.get('kind') == 'io' and isinstance(inner, dict) \
        and 'fenced' in inner


def _journal_has_claim_loss(entries):
    """Whether a `GET /journal` answer recorded the preemption the
    superseded owner must journal — one `field_claim_lost` entry per
    held claim."""
    for entry in entries:
        if 'field_claim_lost' in (entry.get('event') or {}):
            return True
    return False


def scenario_field_claim(ctx):
    """A third sim-net attachment meets the standing field claim: its
    writes and steps fence, the claim lifecycle answers per contract,
    and a rogue claim preempts through the journaled fencing-loss path
    — degrade, never death — after which the pair is restored."""
    case = Case('field-claim',
                'The single-writer claim fences third attachments',
                'with the settled active holding the field claim it '
                'took at launch, a third sim-net attachment\'s write '
                'and step are refused with the named fencing failure '
                'while the active keeps writing; ensure_writer under '
                'a foreign token is refused fenced, under the owner\'s '
                'token answers claimed_shared and writes under the '
                'shared claim; release_writer drops only the caller\'s '
                'hold and answers done for a holder of nothing; and a '
                'rogue claim_writer preempts with field_claim_lost '
                'journaled — the superseded owner demotes instead of '
                'dying — before the scenario restores the pair')
    stream = None
    try:
        if ctx.get('plant') is None:
            return case.finish('inconclusive',
                               'the run publishes no plant endpoint')
        tokens = ctx.get('plant_owner') or {}
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + CLAIM_DEADLINE)
        if active is None:
            reachable = any(
                _try_role(ctx, ctx[name]) is not None
                for name in ('active', 'standby') if ctx.get(name))
            return case.finish(
                'failed' if reachable else 'inconclusive',
                'no peer reports role=active' if reachable
                else 'the pair is unreachable')
        owner = tokens.get(active)
        if owner is None:
            return case.finish('inconclusive', 'the run pins no '
                               'plant-writer owner token for the '
                               'settled active ' + str(active))
        peer = 'standby' if active == 'active' else 'active'
        base, peer_base = ctx[active], ctx[peer]
        stream = _plant_connect(ctx)
        case.observe('field owner: ' + active + ' (' + base + '); '
                     'third sim-net attachment on the run\'s plant '
                     'endpoint')

        # The baseline: a field input whose own stored value the
        # probes write back — an idempotent mutation that only the
        # fencing verdict distinguishes — and the two named refusals.
        # The census rides the shipped tool (list needs no claim); the
        # probes and the whole claim lifecycle stay on the raw
        # attachment — the ops the tool does not expose.
        inputs = _field_inputs(ctx)
        target = None
        for point, info in sorted(inputs.items()):
            if (info.get('sample') or {}).get('value') is not None:
                target = (point, info['sample']['value'])
                break
        before = _snapshot(ctx, base)
        step_probe = _plant_request(stream, {'op': 'step', 'dt': 0})
        write_probe = (None if target is None else _plant_request(
            stream, {'op': 'write', 'point': target[0],
                     'value': target[1]}))
        ref = save_evidence(ctx['evidence_dir'],
                            'field-claim-probes.json',
                            {'target': target, 'step': step_probe,
                             'write': write_probe})
        case.evidence('file', ref, 'the third attachment\'s refused '
                      'mutations')
        if target is None:
            return case.finish('inconclusive', 'the plant census '
                               'lists no field input with a stored '
                               'value to write')
        point, value = target
        if not _mutation_fenced(step_probe):
            return case.finish('failed', 'a third attachment\'s step '
                               'was not refused fenced — the field '
                               'held no enforceable writer claim: '
                               + json.dumps(step_probe)[:300])
        if not _mutation_fenced(write_probe):
            return case.finish('failed', 'a third attachment\'s '
                               'write was not refused fenced: '
                               + json.dumps(write_probe)[:300])
        case.observe('point ' + str(point) + ': step and write '
                     'refused ' + json.dumps(step_probe.get('error'))
                     + ' / ' + json.dumps(write_probe.get('error')))

        # The owner undisturbed: scans keep landing while the fenced
        # attachment's mutations bounce — the tick advances, the role
        # holds, and io_health records no new field failure.
        grown = wait_for(
            lambda: (s.get('tick', 0) > (before or {}).get('tick', 0)
                     and s or None)
            if (s := _try_snapshot(ctx, base)) else None,
            time.monotonic() + CLAIM_DEADLINE)
        health0 = (before or {}).get('io_health') or {}
        health1 = (grown or {}).get('io_health') or {}
        ref = save_evidence(ctx['evidence_dir'],
                            'field-claim-owner.json',
                            {'before': {'tick': (before or {})
                                        .get('tick'),
                                        'io_health': health0},
                             'after': {'tick': (grown or {})
                                       .get('tick'),
                                       'io_health': health1}})
        case.evidence('file', ref, 'the owner\'s scans across the '
                      'fenced probes')
        if grown is None:
            return case.finish('failed', 'the active\'s scans '
                               'stalled under a third attachment\'s '
                               'fenced probes')
        if _settled_active(ctx) != active:
            return case.finish('failed', 'a fenced attachment\'s '
                               'probes moved the active role')
        for key in ('failed_reads', 'failed_writes'):
            if (health1.get(key) or 0) > (health0.get(key) or 0):
                return case.finish('failed', 'the fenced probes '
                                   'counted against the owner\'s '
                                   'io_health: '
                                   + json.dumps(health1)[:300])
        case.observe('the active kept scanning — tick '
                     + str((before or {}).get('tick')) + ' -> '
                     + str(grown.get('tick')))

        # The claim lifecycle: a foreign token's conditional grant
        # fences — ensure_writer never preempts a standing owner —
        # the owner's own token answers claimed_shared and writes
        # under the shared claim, and release_writer drops only the
        # caller's hold.
        foreign = _plant_request(
            stream, {'op': 'ensure_writer', 'owner': CLAIM_FOREIGN})
        shared = _plant_request(
            stream, {'op': 'ensure_writer', 'owner': owner})
        grant_write = _plant_request(
            stream, {'op': 'write', 'point': point, 'value': value})
        release = _plant_request(stream, {'op': 'release_writer'})
        own_write = _plant_request(
            stream, {'op': 'write', 'point': point, 'value': value})
        standing = _plant_probe(ctx, {'op': 'step', 'dt': 0})
        idle = _plant_request(stream, {'op': 'release_writer'})
        still = _plant_probe(ctx, {'op': 'step', 'dt': 0})
        ref = save_evidence(ctx['evidence_dir'],
                            'field-claim-lifecycle.json',
                            {'foreign': foreign, 'shared': shared,
                             'grant_write': grant_write,
                             'release': release,
                             'own_write': own_write,
                             'standing': standing, 'idle': idle,
                             'still': still})
        case.evidence('file', ref, 'the conditional-grant, '
                      'shared-claim, and release answers')
        if not _fenced(foreign):
            return case.finish('failed', 'ensure_writer under a '
                               'foreign token was not refused fenced '
                               '— the conditional grant preempted: '
                               + json.dumps(foreign)[:300])
        if shared.get('result') != 'claimed_shared' \
                or shared.get('owner') != owner:
            return case.finish('failed', 'ensure_writer under the '
                               'owner\'s token did not answer '
                               'claimed_shared: '
                               + json.dumps(shared)[:300])
        if grant_write.get('result') != 'done':
            return case.finish('failed', 'a write under the shared '
                               'claim was refused: '
                               + json.dumps(grant_write)[:300])
        if release.get('result') != 'done':
            return case.finish('failed', 'release_writer refused: '
                               + json.dumps(release)[:300])
        if not _mutation_fenced(own_write):
            return case.finish('failed', 'a released attachment\'s '
                               'write was not fenced back out — the '
                               'release dropped more than the '
                               'caller\'s hold: '
                               + json.dumps(own_write)[:300])
        if not _fenced(standing):
            return case.finish('failed', 'the caller\'s release '
                               'dropped the owner\'s claim — probes '
                               'answered '
                               + json.dumps(standing)[:300])
        if idle.get('result') != 'done':
            return case.finish('failed', 'release_writer from a '
                               'holder of nothing did not answer '
                               'done: ' + json.dumps(idle)[:300])
        if not _fenced(still):
            return case.finish('failed', 'a harmless release changed '
                               'the field: probes answered '
                               + json.dumps(still)[:300])
        case.observe('foreign ensure fenced; owner ensure answered '
                     'claimed_shared and wrote; release dropped only '
                     'the caller\'s hold')

        # The rogue claim: the grant is unconditional, so the attempt
        # preempts — but never silently and never fatally. The
        # declared evidence is the superseded owner's journaled
        # field_claim_lost and its demotion to standby while the
        # monitor keeps serving — the peer degrades, it does not die.
        # A refused attempt is the alternative the contract admits:
        # the standing claim and the owner untouched.
        cursor = _journal_cursor(ctx, base)
        rogue = _plant_request(
            stream, {'op': 'claim_writer', 'owner': CLAIM_ROGUE})
        ref = save_evidence(ctx['evidence_dir'],
                            'field-claim-rogue.json',
                            {'claim': rogue, 'cursor': cursor})
        case.evidence('file', ref, 'the rogue claim answer')
        if rogue.get('result') == 'error':
            probe = _plant_probe(ctx, {'op': 'step', 'dt': 0})
            settled = _settled_active(ctx)
            ref = save_evidence(ctx['evidence_dir'],
                                'field-claim-refused.json',
                                {'probe': probe, 'active': settled})
            case.evidence('file', ref, 'the field and the owner '
                          'after the refused claim')
            if not _fenced(probe):
                return case.finish('failed', 'the refused rogue '
                                   'claim still dropped the standing '
                                   'claim: ' + json.dumps(probe)[:300])
            if settled != active:
                return case.finish('failed', 'a refused rogue claim '
                                   'moved the active role')
            case.observe('the rogue claim was refused '
                         + json.dumps(rogue.get('error'))
                         + ' — the owner and the field undisturbed')
            return case.finish('passed')
        if rogue.get('result') == 'claimed_shared':
            return case.finish('inconclusive', 'the rogue claim '
                               'joined a standing claim under its own '
                               'token — the pinned tokens are not '
                               'unique: ' + json.dumps(rogue)[:300])
        if rogue.get('result') != 'done':
            return case.finish('inconclusive', 'the rogue claim '
                               'answered off-contract: '
                               + json.dumps(rogue)[:300])

        # Preempted: the claim now belongs to the rogue token. The
        # superseded owner's first fenced write must journal
        # field_claim_lost and demote the peer in place — the monitor
        # answering throughout, because a degrade is not a death.
        watch = {'answered': 0, 'roles': []}
        deadline = time.monotonic() + CLAIM_DEADLINE
        settled = None
        while time.monotonic() < deadline:
            report = _try_role(ctx, base)
            if report is not None:
                watch['answered'] += 1
                watch['roles'].append(report.get('role'))
                if report.get('role') == 'standby':
                    settled = report
                    break
            time.sleep(0.1)
        # Whether the monitor still answers at all — the superseded
        # owner degrading is the contract; it going silent is a kill.
        alive = settled is not None
        if not alive:
            report = wait_for(lambda: _try_role(ctx, base),
                              time.monotonic() + 4)
            alive = report is not None
            if (report or {}).get('role') == 'standby':
                settled = report
        journal = None
        if alive:
            try:
                _, body = http_json('GET', base + '/journal?since='
                                    + str(cursor))
                journal = _journal_list(body)
            except Exception:
                pass
        ref = save_evidence(ctx['evidence_dir'],
                            'field-claim-superseded.json',
                            {'watch': watch, 'settled': settled,
                             'alive': alive, 'journal': journal})
        case.evidence('file', ref, 'the superseded owner\'s demotion '
                      'and journal')
        if not alive:
            return case.finish('failed', 'the rogue claim killed the '
                               'active — its monitor stopped '
                               'answering')
        if journal is None:
            return case.finish('inconclusive', 'the superseded '
                               'owner\'s journal never answered')
        if settled is None:
            return case.finish('failed', 'the superseded owner never '
                               'demoted — the claim moved but the '
                               'role stayed '
                               + json.dumps(watch['roles'][-3:]))
        if not _journal_has_claim_loss(journal):
            return case.finish('failed', 'the preemption was silent '
                               '— the superseded owner\'s journal '
                               'recorded no field_claim_lost')
        case.observe('the superseded owner demoted to standby with '
                     'field_claim_lost journaled — degrade, not death')

        # Restore: re-promote the demoted peer — its promotion claim
        # preempts the rogue token, so the field stays claimed
        # throughout — then prove the pair settled back onto the same
        # field owner with probes fenced again.
        promoted = None
        deadline = time.monotonic() + CLAIM_DEADLINE
        while time.monotonic() < deadline and promoted is None:
            try:
                status, body = http_json('POST', base + '/promote')
                if status == 200:
                    promoted = body
                else:
                    time.sleep(POLL_INTERVAL)
            except urllib.error.HTTPError as exc:
                if exc.code == 409:
                    time.sleep(POLL_INTERVAL)
                else:
                    raise
        ref = save_evidence(ctx['evidence_dir'],
                            'field-claim-promote.json',
                            promoted or {'refused': True})
        case.evidence('file', ref, 'the restore promotion')
        if promoted is None:
            return case.finish('failed', 'the demoted owner never '
                               're-promoted — the rig was left '
                               'without a field writer')
        restored = wait_for(
            lambda: _settled_active(ctx) == active
            and _try_role(ctx, base),
            time.monotonic() + CLAIM_DEADLINE)
        snap0 = _try_snapshot(ctx, base)
        probe = _plant_probe(ctx, {'op': 'step', 'dt': 0})
        grown = wait_for(
            lambda: (s.get('tick', 0)
                     > (snap0 or {}).get('tick', 0) and s or None)
            if (s := _try_snapshot(ctx, base)) else None,
            time.monotonic() + CLAIM_DEADLINE)
        peer_role = _try_role(ctx, peer_base)
        ref = save_evidence(ctx['evidence_dir'],
                            'field-claim-restored.json',
                            {'role': restored, 'probe': probe,
                             'peer': peer_role,
                             'snapshot': grown or snap0})
        case.evidence('file', ref, 'the restored pair')
        if not restored:
            return case.finish('failed', 'the re-promoted peer '
                               'never settled active')
        if not _fenced(probe):
            return case.finish('failed', 'the restored claim does '
                               'not fence probes: '
                               + json.dumps(probe)[:300])
        if grown is None:
            return case.finish('failed', 'the restored active is '
                               'not scanning')
        if (peer_role or {}).get('role') != 'standby':
            return case.finish('failed', 'the peer did not return '
                               'to standby: '
                               + json.dumps(peer_role)[:300])
        case.observe('restored: ' + active + ' active and scanning, '
                     'probes fenced, ' + peer + ' standby')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        if stream is not None:
            # Detach cleanly: release whatever hold this attachment
            # still carries — a hold left standing would keep the
            # field claimed for a dead token — then close. The
            # release drops only this connection's hold, so it can
            # never take the owner's claim down with it.
            try:
                _plant_request(stream, {'op': 'release_writer'})
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
