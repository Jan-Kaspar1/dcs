"""The standby_loss acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: The standby-loss case sits in the same restored window: it needs a
# tracking standby to refuse and to lose, drives its own a->b switch
# for the promotion-gate leg, and demote/promotes back to the launch
# roles, so it runs before the tune case's a->b switch.
RUNS_BEFORE = frozenset({'scenario_parameter_tune_carryover'})


# --------------------------------------------------------------------
# WW-LCM-001's peer-lifecycle clauses and WW-OPS-003's tolerated
# interruption on the standby side — the pair coverage's asymmetric
# half: the restart and failover cases already exercise active loss
# (ordered demote/promote, restart recovery), this case exercises
# standby loss. Four legs on the settled pair: a receipted write to
# the tracking standby's monitor answers the named not_active
# rejection with no field effect; the runner-owned
# stop_controller/start_controller pair holds the standby's container
# down — the rig launches controllers with --restart no — while the
# active's scan, role, command path, and journal run undisturbed
# (peer loss is not an event the controller of record reacts to); the
# started standby returns to tracking inside the settle bound; and
# POST /promote fired at its monitor the moment it first answers —
# before its first transfer completes — answers the named
# not_converged refusal, the same promote succeeding once it tracks,
# after which the documented demote/promote order restores the
# pre-scenario role assignment for the cases behind this one.

STANDBY_LOSS_POLL = 0.5            # cadence watching the pair mid-window
STANDBY_LOSS_PROBE = 0.05          # cadence firing /promote at the
                                   # returning monitor — the probe must
                                   # land inside the unsynchronized
                                   # window before the paced cadence
                                   # applies the first checkpoint
STANDBY_LOSS_ROUNDS = 6            # down-window observation rounds
STANDBY_LOSS_REFUSAL_ROUNDS = 3    # post-refusal snapshot rounds
STANDBY_LOSS_RETURN_DEADLINE = 60  # bound on the returning monitor answering
STANDBY_LOSS_SETTLE_DEADLINE = 60  # bound on reconvergence and role settles


def scenario_standby_loss(ctx):
    """Standby loss and the role-gated refusals on the simulated rig:
    a write aimed at the tracking standby is refused not_active with
    no field effect; the standby's container held down never moves the
    active's scan, role, command path, or journal; the returned peer
    reconverges tracking, its first answered promote refuses
    not_converged, and the pair ends on its pre-scenario role
    assignment."""
    case = Case('standby-loss',
                'Standby loss never disturbs the controller of record',
                'a receipted write to the tracking standby\'s monitor '
                'answers the named not_active rejection with the point '
                'unchanged in the active\'s served snapshot, the write '
                'absent from both peers\' adopted receipt logs, and no '
                'journal entry recording it as anything but the '
                'refusal; the stopped standby\'s down-window leaves '
                'the active\'s snapshot tick advancing, its role '
                'active, a receipted command settling, and its journal '
                'free of promotion/demotion entries; the started '
                'standby returns to tracking inside the settle bound, '
                'POST /promote fired the moment its monitor first '
                'answers refuses the named not_converged, the same '
                'promote succeeds once it tracks, and demote/promote '
                'restores the pre-scenario role assignment')
    try:
        stop = ctx.get('stop_controller')
        start = ctx.get('start_controller')
        journals = ctx.get('journal_files') or {}
        if stop is None or start is None:
            return case.finish('inconclusive', 'the run context carries '
                               'no controller stop/start action — the '
                               'standby-loss induction has no '
                               'documented seam')
        # The field writer and its tracking standby — ctx keys, not
        # roles: 'active'/'standby' name the launched containers
        # whichever role each currently reports, so the legs hold on
        # either role layout.
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        if active not in ('active', 'standby'):
            return case.finish('inconclusive', 'the field writer is '
                               + active + ' — outside the launched pair '
                               'the stop action names')
        peer = 'standby' if active == 'active' else 'active'
        base, peer_base = ctx[active], ctx[peer]
        journal = journals.get(active)
        peer_journal = journals.get(peer)
        if journal is None or peer_journal is None:
            return case.finish('inconclusive', 'the run context carries '
                               'no journal-file paths for the pair')
        case.observe('field writer: ' + active + ' (' + base
                     + '); standby-loss target: ' + peer + ' ('
                     + peer_base + ')')

        def tracking(base_url):
            report = _try_role(ctx, base_url)
            if (report or {}).get('role') == 'standby' \
                    and 'tracking' in ((report or {}).get('sync') or {}):
                return report
            return None

        def settled_roles():
            """The pre-scenario role assignment: the original writer
            active, the loss target a tracking standby."""
            owner = _try_role(ctx, base)
            other = _try_role(ctx, peer_base)
            if (owner or {}).get('role') != 'active':
                return None
            if (other or {}).get('role') != 'standby' \
                    or 'tracking' not in ((other or {}).get('sync')
                                          or {}):
                return None
            return {'writer': owner, 'standby': other}

        def control(url, payload=None):
            """POST a control-plane request — /promote, /demote, and
            the refusal leg's /command — returning (status, body) with
            a refused call's named outcome decoded instead of raised."""
            try:
                return http_json('POST', url, payload)
            except urllib.error.HTTPError as exc:
                try:
                    body = json.loads(exc.read() or b'null')
                except ValueError:
                    body = None
                finally:
                    exc.close()
                return exc.code, body

        state = {'stopped': False, 'switched': False}

        def restore_roles():
            """The documented demote/promote order putting the
            pre-scenario role assignment back: the demoted writer
            reconverges tracking behind the promoted peer, the peer
            releases the field, and the tracking writer promotes.
            Returns the failure detail or None."""
            if wait_for(lambda: tracking(base),
                        time.monotonic()
                        + STANDBY_LOSS_SETTLE_DEADLINE,
                        interval=STANDBY_LOSS_POLL) is None:
                return 'the demoted field writer never reconverged ' \
                       'tracking — the restore has no promotable peer'
            status, body = control(peer_base + '/demote')
            if status != 200:
                return 'the restore demote answered ' + str(status) \
                       + ': ' + json.dumps(body)[:300]
            status, body = control(base + '/promote')
            if status != 200:
                return 'the restore promote answered ' + str(status) \
                       + ': ' + json.dumps(body)[:300]
            if wait_for(settled_roles,
                        time.monotonic()
                        + STANDBY_LOSS_SETTLE_DEADLINE,
                        interval=STANDBY_LOSS_POLL) is None:
                return 'the pair did not settle back to its ' \
                       'pre-scenario role assignment'
            return None

        # The tracked baseline: the peer must be a tracking standby —
        # the role-gated legs' observation point.
        if wait_for(lambda: tracking(peer_base),
                    time.monotonic() + 45,
                    interval=STANDBY_LOSS_POLL) is None:
            return case.finish('inconclusive', 'the ' + peer
                               + ' peer is not a tracking standby — '
                               'the role-gated legs have no '
                               'observation point')

        # The command target: the scenarios' writable bool point, read
        # on the field writer for the value the refused write flips.
        _, signals = http_json('GET', base + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'standby-loss-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming writable points')
        target = _writable_bool_point(signals)
        if target is None:
            return case.finish('inconclusive', 'no writable bool '
                               'point in the model')
        point = target['point']
        baseline = _point_value(_snapshot(ctx, base), point)
        if not isinstance(baseline, bool):
            return case.finish('inconclusive', 'point ' + str(point)
                               + ' serves no bool baseline to write '
                               'against')

        def attempt():
            # ---- leg (a): the role-gated command refusal ----
            command = {'command': {'write_value': {
                'point': point, 'kind': 'bool',
                'value': {'bool': not baseline}}},
                'actor': 'qa-lane'}
            status, receipt = control(peer_base + '/command', command)
            ref = save_evidence(ctx['evidence_dir'],
                                'standby-loss-refusal.json',
                                {'command': command, 'status': status,
                                 'receipt': receipt})
            case.evidence('file', ref, 'the standby-directed command\'s '
                          'answer')
            outcome = _outcome_key(receipt)
            case.observe('standby-directed write answered '
                         + str(status) + ' outcome ' + outcome)
            if status != 200 or outcome != 'rejected:not_active':
                return case.finish('failed', 'the standby-directed '
                                   'command did not answer the named '
                                   'not_active rejection: '
                                   + str(status) + ' '
                                   + json.dumps(receipt)[:300])

            # No field effect: the point stands at baseline in the
            # active's served snapshot across the refusal window.
            served = []
            violation = None
            for _ in range(STANDBY_LOSS_REFUSAL_ROUNDS):
                snap = _try_snapshot(ctx, base) or {}
                served.append({'tick': snap.get('tick'),
                               'value': _point_value(snap, point)})
                if served[-1]['value'] != baseline:
                    violation = ('the refused write reached the field: '
                                 'point ' + str(point) + ' serves '
                                 + str(served[-1]['value']))
                    break
                time.sleep(STANDBY_LOSS_POLL)
            # The audit: the write enters neither peer's adopted
            # receipt log; the active's journal carries no record of
            # it at all; the standby's echoes the named rejection
            # alone.
            _, active_served = http_json('GET', base + '/receipts')
            _, peer_served = http_json('GET', peer_base + '/receipts')
            leaked_a = [r for r in _receipt_list(active_served)
                        if r.get('command') == command['command']]
            leaked_p = [r for r in _receipt_list(peer_served)
                        if r.get('command') == command['command']]
            journaled_a = [_journal_settled(item) for item in
                           _journal_entries(journal)
                           if (_journal_settled(item) or {}).get(
                               'command') == command['command']]
            journaled_p = [r for r in (_journal_settled(item)
                                       for item in
                                       _journal_entries(peer_journal))
                           if (r or {}).get('command')
                           == command['command']]
            ref = save_evidence(
                ctx['evidence_dir'], 'standby-loss-refusal-audit.json',
                {'point': point, 'baseline': baseline,
                 'served': served,
                 'receipts_active': leaked_a,
                 'receipts_peer': leaked_p,
                 'journal_active': journaled_a,
                 'journal_peer': journaled_p})
            case.evidence('file', ref, 'the refused write\'s absence '
                          'from the served snapshot, receipt logs, '
                          'and journals')
            if violation is not None:
                return case.finish('failed', violation)
            if leaked_a or leaked_p:
                return case.finish('failed', 'the refused write '
                                   'entered a peer\'s adopted receipt '
                                   'log')
            if journaled_a:
                return case.finish('failed', 'the field writer\'s '
                                   'journal carries a command record '
                                   'for the refused write')
            misnamed = [r for r in journaled_p
                        if _outcome_key(r) != 'rejected:not_active']
            if misnamed:
                return case.finish('failed', 'the standby\'s journal '
                                   'records the refused write as '
                                   'something but the named '
                                   'rejection: '
                                   + json.dumps(misnamed[0])[:300])

            # ---- leg (b): standby-loss non-interference ----
            # The durable journal's baseline before the induction —
            # the diff over the down-window is the contract's proof
            # the controller of record never reacted to the peer loss.
            before_stop = _journal_entries(journal)
            # A receipted command on the active across the window —
            # the write that must still settle.
            window_command = {'command': {'write_value': {
                'point': point, 'kind': 'bool',
                'value': {'bool': baseline}}},
                'actor': 'qa-lane'}
            try:
                stop(peer)
            except Exception as exc:
                return case.finish('inconclusive', 'the standby-stop '
                                   'induction never completed: '
                                   + str(exc)[:300])
            state['stopped'] = True
            case.observe('standby container ' + peer + ' stopped')
            try:
                status, window_receipt = http_json(
                    'POST', base + '/command', window_command)
            except Exception as exc:
                window_receipt = None
                status = str(exc)[:200]
            window = []
            settled = None
            peer_up = False
            last_tick = None
            answered = 0
            for _ in range(STANDBY_LOSS_ROUNDS):
                role = _try_role(ctx, base)
                snap = _try_snapshot(ctx, base)
                tick = (snap or {}).get('tick')
                if role is not None:
                    answered += 1
                try:
                    _role(ctx, peer_base)
                    peer_up = True
                except Exception:
                    pass
                outcome = settled
                if outcome is None:
                    try:
                        _, receipts = http_json('GET',
                                                base + '/receipts')
                        matches = [r for r in _receipt_list(receipts)
                                   if r.get('command')
                                   == window_command['command']]
                        if matches and _outcome_key(matches[-1]) \
                                == 'applied':
                            outcome = 'applied'
                            settled = outcome
                    except Exception:
                        pass
                window.append({'role': (role or {}).get('role'),
                               'tick': tick, 'peer_up': peer_up,
                               'command': outcome})
                if role is not None and role.get('role') != 'active':
                    violation = ('the field writer reported '
                                 + str(role.get('role'))
                                 + ' while the standby was down')
                    break
                if tick is not None and last_tick is not None \
                        and tick <= last_tick:
                    violation = ('the active\'s scan stalled at tick '
                                 + str(tick)
                                 + ' while the standby was down')
                    break
                if tick is not None:
                    last_tick = tick
                time.sleep(STANDBY_LOSS_POLL)
            if isinstance(status, int) and status != 200:
                violation = violation or (
                    'the window command answered ' + str(status))
            elif not isinstance(status, int):
                violation = violation or (
                    'the window command never reached the field '
                    'writer: ' + status)
            elif isinstance(window_receipt, dict) and 'rejected' in \
                    (window_receipt.get('outcome') or {}):
                violation = violation or (
                    'the window command was refused while the '
                    'standby was down: '
                    + json.dumps(window_receipt)[:300])
            # The journal's diff over the window: no role or claim
            # transition, and the window command's settle recorded.
            after = _journal_entries(journal)
            gained = [item['entry'] for item in
                      after[len(before_stop):]
                      if isinstance(item.get('entry'), dict)]
            moved = [entry for entry in gained
                     if 'role_changed' in (entry.get('event') or {})
                     or 'field_claim_lost'
                     in (entry.get('event') or {})]
            journaled_settle = [entry for entry in gained
                                if (_journal_settled(
                                    {'entry': entry}) or {}).get(
                                        'command')
                                == window_command['command']]
            ref = save_evidence(
                ctx['evidence_dir'], 'standby-loss-window.json',
                {'target': peer, 'window': window,
                 'command': window_command,
                 'receipt': window_receipt,
                 'journal_gained': gained[:40],
                 'journal_moved': moved})
            case.evidence('file', ref, 'the down-window observations: '
                          'role, tick, command settle, and the active '
                          'journal\'s diff')
            if peer_up:
                return case.finish('inconclusive', 'the standby '
                                   'answered during the down-window — '
                                   'the stop did not hold a real loss')
            if answered == 0:
                return case.finish('inconclusive', 'the field writer '
                                   'never answered during the window — '
                                   'the induction\'s effect cannot be '
                                   'attributed')
            if violation is not None:
                return case.finish('failed', violation)
            if settled is None:
                return case.finish('failed', 'the window command '
                                   'never settled applied while the '
                                   'standby was down')
            if moved:
                return case.finish('failed', 'the controller of '
                                   'record journaled a role or claim '
                                   'transition for a peer loss it '
                                   'must not react to: '
                                   + json.dumps(moved[0])[:300])
            if not journaled_settle:
                return case.finish('failed', 'the window command '
                                   'settled but its command record '
                                   'never reached the journal')
            case.observe('the standby held down ' + str(len(window))
                         + ' rounds: the writer stayed active, its '
                         'tick advanced, the window command settled, '
                         'no role entries journaled')

            # ---- legs (c)+(d): return, reconvergence, the gate ----
            try:
                start(peer)
            except Exception as exc:
                return case.finish('inconclusive', 'the standby-start '
                                   'action never completed: '
                                   + str(exc)[:300])
            state['stopped'] = False
            case.observe('standby container ' + peer + ' started')
            # The promotion gate: POST /promote is itself the probe —
            # the first answered request lands the moment the
            # returning monitor serves, before the paced cadence has
            # applied a checkpoint, and the peer's standing is still
            # unsynchronized.
            first = None
            deadline = time.monotonic() + STANDBY_LOSS_RETURN_DEADLINE
            while time.monotonic() < deadline and first is None:
                try:
                    status, body = http_json('POST',
                                             peer_base + '/promote')
                    first = {'status': status, 'body': body}
                except urllib.error.HTTPError as exc:
                    try:
                        body = json.loads(exc.read() or b'null')
                    except ValueError:
                        body = None
                    finally:
                        exc.close()
                    first = {'status': exc.code, 'body': body}
                except Exception:
                    time.sleep(STANDBY_LOSS_PROBE)
            ref = save_evidence(ctx['evidence_dir'],
                                'standby-loss-promote-gate.json',
                                {'first_promote': first})
            case.evidence('file', ref, 'the first promote the '
                          'returning standby answered')
            if first is None:
                return case.finish('inconclusive', 'the returned '
                                   'standby\'s monitor never answered '
                                   'inside '
                                   + str(STANDBY_LOSS_RETURN_DEADLINE)
                                   + 's')
            if first['status'] == 200:
                # Promoted without the named refusal ever preceding —
                # the gate the issue asserts was never exercised. The
                # pair is already switched; restore it, then fail.
                state['switched'] = True
                restore_detail = restore_roles()
                if restore_detail is None:
                    state['switched'] = False
                detail = ('the returning standby promoted on its '
                          'first answered request — no not_converged '
                          'refusal preceded the switch')
                if restore_detail is not None:
                    detail += '; the restore also failed: ' \
                              + restore_detail
                return case.finish('failed', detail)
            named = (first['body'] or {}).get('not_converged') \
                if isinstance(first['body'], dict) else None
            if first['status'] != 409 or not isinstance(named, dict):
                return case.finish('failed', 'the first promote on '
                                   'the returning standby answered '
                                   + str(first['status']) + ' '
                                   + json.dumps(first['body'])[:300]
                                   + ' — not the named not_converged '
                                   'refusal')
            case.observe('promotion gate held: the returning '
                         'standby\'s first promote answered '
                         'not_converged')

            # Reconvergence: the returned standby's paced pulls carry
            # it to tracking inside the settle bound.
            reconverged = wait_for(
                lambda: tracking(peer_base),
                time.monotonic() + STANDBY_LOSS_SETTLE_DEADLINE,
                interval=STANDBY_LOSS_POLL)
            ref = save_evidence(ctx['evidence_dir'],
                                'standby-loss-reconverged.json',
                                reconverged)
            case.evidence('file', ref, 'the returned standby\'s '
                          'tracking report')
            if reconverged is None:
                return case.finish('failed', 'the returned standby '
                                   'never reached tracking '
                                   'convergence inside '
                                   + str(STANDBY_LOSS_SETTLE_DEADLINE)
                                   + 's')

            # Once tracking, promotion succeeds — the documented
            # switch: demote the standing writer, then promote the
            # converged peer.
            status, body = control(base + '/demote')
            if status != 200:
                return case.finish('failed', 'the switch demote '
                                   'answered ' + str(status) + ': '
                                   + json.dumps(body)[:300])
            status, body = control(peer_base + '/promote')
            if status != 200:
                return case.finish('failed', 'the converged peer\'s '
                                   'promote answered ' + str(status)
                                   + ': ' + json.dumps(body)[:300])
            state['switched'] = True

            def promoted_role():
                report = _try_role(ctx, peer_base)
                if (report or {}).get('role') == 'active':
                    return report
                return None

            promoted = wait_for(
                promoted_role,
                time.monotonic() + STANDBY_LOSS_SETTLE_DEADLINE,
                interval=STANDBY_LOSS_POLL)
            ref = save_evidence(ctx['evidence_dir'],
                                'standby-loss-switched.json',
                                {'promoted': promoted})
            case.evidence('file', ref, 'the promoted peer settling '
                          'active')
            if promoted is None:
                return case.finish('failed', 'the promoted peer '
                                   'never settled active')

            # Restore: demote/promote returns the pair to its
            # pre-scenario role assignment for the cases behind this
            # one.
            detail = restore_roles()
            if detail is None:
                state['switched'] = False
            final = settled_roles()
            ref = save_evidence(ctx['evidence_dir'],
                                'standby-loss-restored.json', final)
            case.evidence('file', ref, 'the restored role assignment')
            if detail is not None:
                return case.finish('failed', 'the pair was not '
                                   'restored to its pre-scenario role '
                                   'assignment: ' + detail)
            if final is None:
                return case.finish('failed', 'the pair did not '
                                   'return to its pre-scenario role '
                                   'assignment')
            return case.finish('passed')

        try:
            return attempt()
        finally:
            # Whatever the legs left behind on an early exit — a
            # stopped peer or a switched pair — put it back for the
            # cases behind this one, best-effort.
            if state['stopped']:
                try:
                    start(peer)
                    case.observe('cleanup: standby container '
                                 + peer + ' restarted')
                except Exception as exc:
                    case.observe('cleanup: the standby restart '
                                 'failed: ' + str(exc)[:200])
            if state['switched']:
                try:
                    detail = restore_roles()
                except Exception as exc:
                    detail = str(exc)[:200]
                case.observe('cleanup: role restore '
                             + (detail or 'completed'))
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
