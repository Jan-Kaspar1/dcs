"""The force_carryover acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *


# --------------------------------------------------------------------
# The receipted force set as carried run state (WW-LCM-001's
# continuity clause, WW-OPS-003's substituted-quality semantics,
# decision 21's checkpoint-carried run state): `force_point` on the
# same writable `In` point the static-active case uses rides the
# checkpoint stream — the tracking standby's own snapshot reports the
# badge because its scans run the adopted state — and a promoted peer
# must inherit the force rather than silently releasing it. The
# release on the promoted peer settles applied and the held-value
# rule resumes: the last-stamped sample re-stamps Good. The case runs
# inside the pre-switch window — a settled active plus a tracking
# peer — on either role layout (ctrl-a/ctrl-b ahead of the tune
# case's switch, or the mirrored post-failover pair a replay finds),
# and it restores the launch role assignment through the documented
# demote/promote fail-back before later cases run.


def scenario_force_carryover(ctx):
    """A receipted force riding the checkpoint survives the pair's
    promotion; the release on the promoted peer settles applied and
    the held-value rule resumes at Good — then the pair returns to
    its launch role assignment."""
    case = Case('force-carryover',
                'Receipted force carries across promotion',
                'force_point on the writable p101-oos point badges the '
                'point under snapshot.forces at Uncertain(Substituted) '
                'on the active AND on the tracking standby\'s own '
                'snapshot; the promoted peer still badges it at the '
                'forced value and substituted quality with scans '
                'advancing; unforce_point on the new active settles '
                'applied, clears the badge, and the point resumes its '
                'unforced value at Good; the pair returns to its '
                'pre-scenario role assignment')
    try:
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        peer = 'standby' if active == 'active' else 'active'
        base, peer_base = ctx[active], ctx[peer]
        case.observe('forcing against ' + active + ' (' + base
                     + '); tracking peer ' + peer + ' (' + peer_base
                     + ')')

        def converged():
            try:
                report = _role(ctx, peer_base)
            except Exception:
                return None
            sync = report.get('sync') or {}
            return report if 'tracking' in sync else None

        tracking = wait_for(converged, time.monotonic() + FORCE_DEADLINE)
        ref = save_evidence(ctx['evidence_dir'],
                            'force-carryover-peer-role.json',
                            {'peer': peer, 'report': tracking})
        case.evidence('file', ref, 'the tracking peer\'s role report')
        if not tracking:
            return case.finish('inconclusive', 'the peer never '
                               'reported tracking convergence — the '
                               'carryover cannot be exercised')

        _, signals = http_json('GET', base + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'force-carryover-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming the force '
                      'target')
        target = None
        for entry in signals.get('points', []):
            if entry.get('name') == 'p101-oos' and entry.get('writable') \
                    and entry.get('direction') == 'in':
                target = entry.get('point')
                break
        if target is None:
            return case.finish('inconclusive', 'the rig model lacks '
                               'the writable p101-oos point')

        baseline = _snapshot(ctx, base)
        held = _point_value(baseline, target)
        if not isinstance(held, bool):
            return case.finish(
                'inconclusive',
                'the force target holds no bool baseline: '
                + json.dumps(_point_sample(baseline, target))[:300])
        forced_value = not held
        case.observe('force target: p101-oos point ' + str(target)
                     + ' held ' + str(held) + '; forcing '
                     + str(forced_value))

        force_body = {'point': target, 'kind': 'bool',
                      'value': {'bool': forced_value}}
        status, receipt = http_json(
            'POST', base + '/command',
            {'command': {'force_point': force_body}, 'actor': 'qa-lane'})
        ref = save_evidence(ctx['evidence_dir'],
                            'force-carryover-force-receipt.json',
                            {'status': status, 'body': receipt})
        case.evidence('file', ref, 'the force submission receipt')
        outcome = (receipt or {}).get('outcome') or {}
        if status != 200 or 'rejected' in outcome:
            return case.finish('failed', 'force refused: ' + str(status)
                               + ' ' + json.dumps(receipt)[:400])
        case.observe('force admitted: '
                     + json.dumps(outcome, sort_keys=True))

        observed = {}

        def forced_state():
            try:
                snap = _snapshot(ctx, base)
            except Exception:
                return None
            observed['forced'] = snap
            badge = _forced_entry(snap, target)
            if _point_value(snap, target) == forced_value \
                    and _point_quality(snap, target) \
                    == {'uncertain': 'substituted'} \
                    and (badge or {}).get('value') \
                    == {'bool': forced_value}:
                return snap
            return None

        forced = wait_for(forced_state,
                          time.monotonic() + FORCE_DEADLINE)
        ref = save_evidence(ctx['evidence_dir'],
                            'force-carryover-forced.json',
                            observed.get('forced') or {})
        case.evidence('file', ref, 'the active\'s snapshot while the '
                      'force stands')
        if forced is None:
            snap = observed.get('forced') or {}
            unmet = []
            if _point_value(snap, target) != forced_value:
                unmet.append('the forced value ' + str(forced_value))
            if _point_quality(snap, target) \
                    != {'uncertain': 'substituted'}:
                unmet.append('Uncertain(Substituted) quality')
            if (_forced_entry(snap, target) or {}).get('value') \
                    != {'bool': forced_value}:
                unmet.append('a snapshot.forces entry')
            return case.finish('failed', 'forced telemetry never '
                               'showed ' + ' + '.join(unmet))
        case.observe('forced on the active: point ' + str(target)
                     + ' reads ' + str(forced_value)
                     + ' at Uncertain(Substituted), badged under '
                     'snapshot.forces')

        # The standby's own snapshot must report the same force while
        # it tracks — its scans run the adopted state the checkpoint
        # carries, badge and substituted stamp included.
        def peer_forced():
            try:
                snap = _snapshot(ctx, peer_base)
            except Exception:
                return None
            observed['peer_forced'] = snap
            badge = _forced_entry(snap, target)
            if _point_value(snap, target) == forced_value \
                    and _point_quality(snap, target) \
                    == {'uncertain': 'substituted'} \
                    and (badge or {}).get('value') \
                    == {'bool': forced_value}:
                return snap
            return None

        peer_snap = wait_for(peer_forced,
                             time.monotonic() + FORCE_DEADLINE)
        ref = save_evidence(ctx['evidence_dir'],
                            'force-carryover-peer-forced.json',
                            observed.get('peer_forced') or {})
        case.evidence('file', ref, 'the tracking standby\'s own '
                      'snapshot while the force stands')
        if peer_snap is None:
            return case.finish('failed', 'the tracking standby never '
                               'reported the force on its own snapshot '
                               '— the adopted state dropped the badge')
        case.observe('the tracking standby reports the same force at '
                     'Uncertain(Substituted) — the badge rides the '
                     'checkpoint')

        # The switch: demote the forced active, promote the converged
        # standby — the receipted run state must cross with it.
        status, body = http_json('POST', base + '/demote')
        case.observe('demote ' + active + ': ' + str(status) + ' '
                     + json.dumps(body))
        if status != 200:
            return case.finish('failed', 'demote refused: '
                               + str(body))
        promoted = None
        deadline = time.monotonic() + FORCE_DEADLINE
        while time.monotonic() < deadline and promoted is None:
            try:
                status, body = http_json('POST', peer_base + '/promote')
                if status == 200:
                    promoted = body
                else:
                    time.sleep(POLL_INTERVAL)
            except urllib.error.HTTPError as exc:
                if exc.code == 409:
                    time.sleep(POLL_INTERVAL)
                else:
                    raise
        settled_role = wait_for(
            lambda: (r.get('role') == 'active' and r or None)
            if (r := _role(ctx, peer_base)) else None,
            time.monotonic() + FORCE_DEADLINE)
        ref = save_evidence(ctx['evidence_dir'],
                            'force-carryover-promotion.json',
                            {'demoted': active, 'promote': promoted,
                             'role': settled_role})
        case.evidence('file', ref, 'the demote/promote responses and '
                      'the promoted peer\'s role')
        if promoted is None:
            return case.finish('failed', 'the converged standby never '
                               'promoted within ' + str(FORCE_DEADLINE)
                               + 's')
        if not settled_role:
            return case.finish('failed', 'the promoted peer did not '
                               'settle active')

        # The promoted peer: the badge must still name the point at
        # the forced value and substituted quality while its scans
        # keep advancing — a force silently cleared or reverted at
        # the promotion boundary fails here.
        first = _try_snapshot(ctx, peer_base) or {}

        def carried():
            try:
                snap = _snapshot(ctx, peer_base)
            except Exception:
                return None
            observed['carried'] = snap
            badge = _forced_entry(snap, target)
            if _point_value(snap, target) == forced_value \
                    and _point_quality(snap, target) \
                    == {'uncertain': 'substituted'} \
                    and (badge or {}).get('value') \
                    == {'bool': forced_value} \
                    and snap.get('tick', 0) > first.get('tick', 0):
                return snap
            return None

        carried_snap = wait_for(carried,
                                time.monotonic() + FORCE_DEADLINE)
        ref = save_evidence(ctx['evidence_dir'],
                            'force-carryover-promoted.json',
                            observed.get('carried') or first)
        case.evidence('file', ref, 'the promoted peer\'s snapshot')
        if carried_snap is None:
            snap = observed.get('carried') or first
            unmet = []
            if (_forced_entry(snap, target) or {}).get('value') \
                    != {'bool': forced_value}:
                unmet.append('the forces badge for point '
                             + str(target))
            if _point_value(snap, target) != forced_value:
                unmet.append('the forced value ' + str(forced_value))
            if _point_quality(snap, target) \
                    != {'uncertain': 'substituted'}:
                unmet.append('Uncertain(Substituted) quality')
            if not snap or snap.get('tick', 0) <= first.get('tick', 0):
                unmet.append('advancing scans')
            return case.finish('failed', 'the promoted peer lost '
                               + ' + '.join(unmet)
                               + ' — the force did not ride the '
                               'checkpoint')
        case.observe('the promoted peer still badges point '
                     + str(target) + ' at ' + str(forced_value)
                     + '/substituted, tick ' + str(first.get('tick'))
                     + ' -> ' + str(carried_snap.get('tick')))

        # The release on the new active: a settled `applied` receipt,
        # an empty forces list, and the held-value rule resuming at
        # Good — the force's last stamp persists as the held sample.
        index = _next_receipt_index(ctx, peer_base)
        unforce_body = {'point': target}
        status, receipt = http_json(
            'POST', peer_base + '/command',
            {'command': {'unforce_point': unforce_body},
             'actor': 'qa-lane'})
        ref = save_evidence(ctx['evidence_dir'],
                            'force-carryover-release-receipt.json',
                            {'status': status, 'body': receipt})
        case.evidence('file', ref, 'the release submission receipt')
        outcome = (receipt or {}).get('outcome') or {}
        if status != 200 or 'rejected' in outcome:
            return case.finish('failed', 'release refused: '
                               + str(status) + ' '
                               + json.dumps(receipt)[:400])
        settled = wait_for(
            lambda: _settled_outcome(ctx, peer_base, index),
            time.monotonic() + FORCE_DEADLINE)
        if settled != 'applied':
            return case.finish('failed', 'the release on the promoted '
                               'peer did not settle applied: '
                               + str(settled or 'never settled'))

        def released():
            try:
                snap = _snapshot(ctx, peer_base)
            except Exception:
                return None
            observed['released'] = snap
            if _forced_entry(snap, target) is None \
                    and _point_value(snap, target) == forced_value \
                    and _point_quality(snap, target) == 'good':
                return snap
            return None

        if not wait_for(released, time.monotonic() + FORCE_DEADLINE):
            snap = observed.get('released') or {}
            unmet = []
            if _forced_entry(snap, target) is not None:
                unmet.append('an empty forces list')
            if _point_value(snap, target) != forced_value:
                unmet.append('the held value ' + str(forced_value))
            if _point_quality(snap, target) != 'good':
                unmet.append('Good quality (the point re-substituted?)')
            return case.finish('failed', 'the released point did not '
                               'recover: ' + ' + '.join(unmet))
        ref = save_evidence(ctx['evidence_dir'],
                            'force-carryover-released.json',
                            observed.get('released') or {})
        case.evidence('file', ref, 'the snapshot after the release '
                      'settled')
        case.observe('released on the promoted peer: forces cleared, '
                     'point ' + str(target) + ' reads '
                     + str(forced_value) + ' at Good')

        # The run's audit on the promoted peer: the release settles
        # journaled as applied, attributed to qa-lane.
        found = {}

        def journaled():
            try:
                _, journal = http_json('GET', peer_base
                                       + '/journal?since=0')
            except Exception:
                return None
            observed['journal'] = journal
            for entry in _settled_receipts(journal):
                if (entry.get('command') or {}) \
                        .get('unforce_point') == unforce_body:
                    found['release'] = entry
            return found.get('release') is not None or None

        wait_for(journaled, time.monotonic() + FORCE_DEADLINE)
        ref = save_evidence(ctx['evidence_dir'],
                            'force-carryover-journal.json',
                            observed.get('journal') or [])
        case.evidence('file', ref, 'the promoted peer\'s journal')
        entry = found.get('release')
        if entry is None:
            return case.finish('failed', 'no settled release receipt '
                               'journaled on the promoted peer')
        if entry.get('actor') != 'qa-lane':
            return case.finish('failed', 'the release receipt is '
                               'unattributed (actor='
                               + json.dumps(entry.get('actor')) + ')')
        if 'applied' not in (entry.get('outcome') or {}):
            return case.finish('failed', 'the release receipt did not '
                               'settle applied: '
                               + json.dumps(entry.get('outcome'))[:200])

        # Restore the pre-scenario role assignment: demote the new
        # active, promote the reconverged original back — the
        # documented fail-back the suite's later cases rely on.
        status, body = http_json('POST', peer_base + '/demote')
        case.observe('restore demote ' + peer + ': ' + str(status)
                     + ' ' + json.dumps(body))
        if status != 200:
            return case.finish('failed', 'the restore demote was '
                               'refused: ' + str(body))
        restored = None
        deadline = time.monotonic() + FORCE_DEADLINE
        while time.monotonic() < deadline and restored is None:
            try:
                status, body = http_json('POST', base + '/promote')
                if status == 200:
                    restored = body
                else:
                    time.sleep(POLL_INTERVAL)
            except urllib.error.HTTPError as exc:
                if exc.code == 409:
                    time.sleep(POLL_INTERVAL)
                else:
                    raise
        settled_back = wait_for(
            lambda: (r.get('role') == 'active' and r or None)
            if (r := _role(ctx, base)) else None,
            time.monotonic() + FORCE_DEADLINE)
        # The launch assignment is a role pair, not one role: the
        # demoted successor must be back tracking the restored active,
        # or the next carryover leg has no converged peer to promote.
        peer_back = wait_for(
            lambda: (r.get('role') == 'standby'
                     and 'tracking' in (r.get('sync') or {})
                     and r or None)
            if (r := _role(ctx, peer_base)) else None,
            time.monotonic() + FORCE_DEADLINE)
        ref = save_evidence(ctx['evidence_dir'],
                            'force-carryover-restored.json',
                            {'demoted': peer, 'promote': restored,
                             'role': settled_back, 'peer': peer_back})
        case.evidence('file', ref, 'the fail-back responses and the '
                      'restored roles')
        if restored is None or not settled_back or not peer_back:
            return case.finish('failed', 'the pair is not restored '
                               'to its pre-scenario role assignment')
        case.observe('restored: ' + active + ' reports active again, '
                     + peer + ' returns to tracking')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
