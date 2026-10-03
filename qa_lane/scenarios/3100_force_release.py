"""The force_release acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *


def _release_recovery_unmet(snap, target, follower, value, cone_value):
    """The unmet clauses of the post-release recovery contract on one
    served snapshot — the badge gone, the released point holding the
    force's last stamp at Good, the inverted cone untainted at the
    unforced read. Empty when the observation shows the recovered
    state."""
    unmet = []
    if _forced_entry(snap, target) is not None:
        unmet.append('an empty forces list')
    if _point_value(snap, target) != value:
        unmet.append('the held image at ' + str(value))
    if _point_quality(snap, target) != 'good':
        unmet.append('Good quality — the point reads '
                     + json.dumps(_point_quality(snap, target)))
    if _point_value(snap, follower) != cone_value:
        unmet.append('p101-oos-ok reading ' + str(cone_value))
    if _point_quality(snap, follower) != 'good':
        unmet.append('the p101-oos-ok cone untainted')
    return unmet


def scenario_force_release(ctx):
    """A receipted force pins p101-oos at Substituted quality with the
    control image following it; its release re-stamps the held image
    Good on the active and on the tracking standby's adopted
    snapshot, then the restore write returns the pre-force held
    value — every command journaled as a settled, attributed
    receipt."""
    case = Case('force-release',
                'Receipted forcing and release on a writable point',
                'force_point on the writable p101-oos point serves the '
                'forced value at Uncertain(Substituted), lists the '
                'point under snapshot.forces, and the inverted '
                'p101-oos-ok carrier follows the forced value; '
                'unforce_point clears the badge and re-stamps the '
                'held image Good on the same observation — the '
                'released sample reads the persisted stamp and the '
                'p101-oos-ok cone untaints, and the tracking '
                'standby\'s adopted snapshot shows the same '
                'post-release state — before the restore write '
                'returns the pre-force held value; both commands '
                'journal as settled receipts attributed to qa-lane')
    try:
        # Self-contained on either role layout, like evidence-capture:
        # replayed alone the rig is fresh (ctrl-a active), while the
        # full suite reaches this case after the failover.
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        peer = 'standby' if active == 'active' else 'active'
        base, peer_base = ctx[active], ctx[peer]
        case.observe('forcing against ' + active + ' (' + base
                     + '); tracking peer ' + peer + ' (' + peer_base
                     + ')')

        # The standby-parity leg needs a settled pair: without a
        # tracking peer the adopted-state check cannot be exercised.
        def converged():
            try:
                report = _role(ctx, peer_base)
            except Exception:
                return None
            sync = report.get('sync') or {}
            return report if 'tracking' in sync else None

        tracking = wait_for(converged, time.monotonic() + FORCE_DEADLINE)
        ref = save_evidence(ctx['evidence_dir'],
                            'force-release-peer-role.json',
                            {'peer': peer, 'report': tracking})
        case.evidence('file', ref, 'the tracking peer\'s role report')
        if not tracking:
            return case.finish('inconclusive', 'the peer never '
                               'reported tracking convergence — the '
                               'standby parity leg cannot be exercised')

        _, signals = http_json('GET', base + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'force-release-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming the force target')
        target = follower = None
        for entry in signals.get('points', []):
            if entry.get('name') == 'p101-oos' and entry.get('writable') \
                    and entry.get('direction') == 'in':
                target = entry.get('point')
            elif entry.get('name') == 'p101-oos-ok':
                follower = entry.get('point')
        if target is None or follower is None:
            return case.finish(
                'inconclusive',
                'the rig model lacks the writable p101-oos point or '
                'its p101-oos-ok in-service carrier')

        # The held value the release leg restores — whatever the run's
        # earlier commands left the operator point holding.
        baseline = _snapshot(ctx, base)
        held = _point_value(baseline, target)
        if not isinstance(held, bool):
            return case.finish(
                'inconclusive',
                'the force target holds no bool baseline: '
                + json.dumps(_point_sample(baseline, target))[:300])
        forced_value = not held
        case.observe('force target: p101-oos point ' + str(target)
                     + ' held ' + str(held) + '; control probe '
                     'p101-oos-ok point ' + str(follower)
                     + ' (the inverted in-service carrier)')

        force_body = {'point': target, 'kind': 'bool',
                      'value': {'bool': forced_value}}
        status, receipt = http_json(
            'POST', base + '/command',
            {'command': {'force_point': force_body}, 'actor': 'qa-lane'})
        ref = save_evidence(ctx['evidence_dir'],
                            'force-release-force-receipt.json',
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
                    == {'bool': forced_value} \
                    and _point_value(snap, follower) == held:
                return snap
            return None

        forced = wait_for(forced_state, time.monotonic() + FORCE_DEADLINE)
        ref = save_evidence(ctx['evidence_dir'],
                            'force-release-forced.json',
                            observed.get('forced') or {})
        case.evidence('file', ref, 'snapshot while the force stands')
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
            if _point_value(snap, follower) != held:
                unmet.append('control following the force '
                             '(p101-oos-ok reading ' + str(held) + ')')
            return case.finish('failed', 'forced telemetry never '
                               'showed ' + ' + '.join(unmet))
        case.observe('forced: point ' + str(target) + ' reads '
                     + str(forced_value)
                     + ' at Uncertain(Substituted), badged under '
                     'snapshot.forces; p101-oos-ok follows at '
                     + str(held))

        unforce_body = {'point': target}
        status, receipt = http_json(
            'POST', base + '/command',
            {'command': {'unforce_point': unforce_body},
             'actor': 'qa-lane'})
        ref = save_evidence(ctx['evidence_dir'],
                            'force-release-release-receipt.json',
                            {'status': status, 'body': receipt})
        case.evidence('file', ref, 'the release submission receipt')
        outcome = (receipt or {}).get('outcome') or {}
        if status != 200 or 'rejected' in outcome:
            return case.finish('failed', 'release refused: '
                               + str(status) + ' '
                               + json.dumps(receipt)[:400])
        case.observe('release admitted: '
                     + json.dumps(outcome, sort_keys=True))

        def released():
            try:
                snap = _snapshot(ctx, base)
            except Exception:
                return None
            observed['released'] = snap
            return _forced_entry(snap, target) is None and snap

        cleared = wait_for(released, time.monotonic() + FORCE_DEADLINE)
        ref = save_evidence(ctx['evidence_dir'],
                            'force-release-released.json',
                            observed.get('released') or {})
        case.evidence('file', ref, 'snapshot after the release settled')
        if not cleared:
            return case.finish('failed',
                               'the forces badge never cleared after '
                               'unforce_point')

        # Finding #498's pinned regression: the release boundary
        # re-stamps the held image Good — the force's last stamp
        # persists as the held sample — and the inverted p101-oos-ok
        # cone untaints on the same observation. A released point
        # still stamped Substituted here is the stuck image the
        # finding reported; the restore write below must not be what
        # papers it over.
        def release_recovered():
            try:
                snap = _snapshot(ctx, base)
            except Exception:
                return None
            observed['release_recovered'] = snap
            return not _release_recovery_unmet(
                snap, target, follower, forced_value, held) and snap

        if not wait_for(release_recovered,
                        time.monotonic() + FORCE_DEADLINE):
            unmet = _release_recovery_unmet(
                observed.get('release_recovered') or {},
                target, follower, forced_value, held)
            return case.finish('failed', 'the released point did not '
                               'recover: ' + ' + '.join(unmet))
        ref = save_evidence(ctx['evidence_dir'],
                            'force-release-release-recovered.json',
                            observed.get('release_recovered') or {})
        case.evidence('file', ref, 'the released snapshot before the '
                      'restore write — held image at Good, cone '
                      'untainted')
        case.observe('released: point ' + str(target) + ' reads '
                     + str(forced_value) + ' at Good with the '
                     'p101-oos-ok cone untainted at ' + str(held)
                     + ' — no restamp write needed')

        # The finding tainted the tracking standby too: its adopted
        # snapshot must carry the same post-release state — never a
        # permanently Substituted cone on either peer.
        def peer_recovered():
            try:
                snap = _snapshot(ctx, peer_base)
            except Exception:
                return None
            observed['peer_recovered'] = snap
            return not _release_recovery_unmet(
                snap, target, follower, forced_value, held) and snap

        if not wait_for(peer_recovered,
                        time.monotonic() + FORCE_DEADLINE):
            unmet = _release_recovery_unmet(
                observed.get('peer_recovered') or {},
                target, follower, forced_value, held)
            return case.finish('failed', 'the tracking standby\'s '
                               'adopted snapshot never showed the '
                               'released state: ' + ' + '.join(unmet))
        ref = save_evidence(ctx['evidence_dir'],
                            'force-release-peer-recovered.json',
                            observed.get('peer_recovered') or {})
        case.evidence('file', ref, 'the tracking standby\'s adopted '
                      'snapshot carrying the same recovered state')
        case.observe('the tracking standby\'s adopted snapshot '
                     'matches: point ' + str(target) + ' at Good, '
                     'the cone untainted')

        # The restore step: the release is proven above, so this
        # receipted write only restamps the pre-force held value —
        # later scenarios find the rig in its prior state.
        status, receipt = http_json(
            'POST', base + '/command',
            {'command': {'write_value': {
                'point': target, 'kind': 'bool',
                'value': {'bool': held}}},
             'actor': 'qa-lane'})
        ref = save_evidence(ctx['evidence_dir'],
                            'force-release-restore-receipt.json',
                            {'status': status, 'body': receipt})
        case.evidence('file', ref, 'the restore-write submission receipt')
        outcome = (receipt or {}).get('outcome') or {}
        if status != 200 or 'rejected' in outcome:
            return case.finish('failed', 'the restore write was '
                               'refused: ' + str(status) + ' '
                               + json.dumps(receipt)[:400])

        def recovered():
            try:
                snap = _snapshot(ctx, base)
            except Exception:
                return None
            observed['recovered'] = snap
            if _point_value(snap, target) == held \
                    and _point_quality(snap, target) == 'good' \
                    and _forced_entry(snap, target) is None \
                    and _point_value(snap, follower) == forced_value:
                return snap
            return None

        if not wait_for(recovered, time.monotonic() + FORCE_DEADLINE):
            snap = observed.get('recovered') or {}
            unmet = []
            if _point_value(snap, target) != held:
                unmet.append('the held value ' + str(held))
            if _point_quality(snap, target) != 'good':
                unmet.append('Good quality')
            if _forced_entry(snap, target) is not None:
                unmet.append('an empty forces list')
            if _point_value(snap, follower) != forced_value:
                unmet.append('control recovering (p101-oos-ok reading '
                             + str(forced_value) + ')')
            return case.finish('failed', 'telemetry did not recover '
                               'after release: ' + ' + '.join(unmet))
        ref = save_evidence(ctx['evidence_dir'],
                            'force-release-recovered.json',
                            observed.get('recovered') or {})
        case.evidence('file', ref, 'snapshot after the restore write')
        case.observe('released and restored: point ' + str(target)
                     + ' reads ' + str(held) + ' at Good, forces '
                     'cleared, p101-oos-ok back at '
                     + str(forced_value))

        # Both commands must journal as settled receipts carrying the
        # run's actor — the audit half of the receipted-command
        # contract.
        found = {'force': None, 'release': None}

        def settled():
            try:
                _, journal = http_json('GET', base + '/journal?since=0')
            except Exception:
                return None
            observed['journal'] = journal
            for entry in _settled_receipts(journal):
                command = entry.get('command') or {}
                if command.get('force_point') == force_body:
                    found['force'] = entry
                elif command.get('unforce_point') == unforce_body:
                    found['release'] = entry
            return (found['force'] is not None
                    and found['release'] is not None) or None

        wait_for(settled, time.monotonic() + FORCE_DEADLINE)
        ref = save_evidence(ctx['evidence_dir'],
                            'force-release-journal.json',
                            observed.get('journal') or [])
        case.evidence('file', ref, 'journal tail with the settled '
                      'receipts')
        unmet = []
        for name, entry in (('force', found['force']),
                            ('release', found['release'])):
            if entry is None:
                unmet.append('no settled ' + name
                             + ' receipt journaled')
                continue
            if entry.get('actor') != 'qa-lane':
                unmet.append('the ' + name + ' receipt is unattributed '
                             '(actor='
                             + json.dumps(entry.get('actor')) + ')')
            if 'applied' not in (entry.get('outcome') or {}):
                unmet.append('the ' + name + ' receipt did not settle '
                             'applied: '
                             + json.dumps(entry.get('outcome'))[:200])
        if unmet:
            return case.finish('failed', 'journal audit: '
                               + '; '.join(unmet))
        case.observe('journal: force and release settled as applied '
                     'receipts attributed to qa-lane')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
