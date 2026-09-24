"""The checkpoint_negotiation acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *


# --------------------------------------------------------------------
# The named rejection of incompatible state — WW-LCM-001's
# checkpoint-negotiation clause: a third controller launched
# --standby <active> on a document whose model fingerprint differs
# from the pair's (the recipe-derived revised model WITHOUT the
# --revised opt-in) must never converge — every pulled checkpoint meets
# the fingerprint gate's named refusal, the peer reports degraded for
# the observation window, and POST /promote answers the named
# not_converged refusal — while the active peer's ticks, field writes,
# and receipt log continue undisturbed. The case then removes the
# foreign container so later cases — the model-revision launch above
# all — see a clean rig. It runs while the pair still runs the mounted
# model: after the revision roll the same document would no longer be
# foreign.

NEGOTIATION_DEADLINE = 60   # bound on the named degraded report
NEGOTIATION_ROUNDS = 6      # observation-window polls once degraded
NEGOTIATION_POLL = 0.5      # cadence watching the foreign peer


def scenario_checkpoint_negotiation(ctx):
    """A foreign-fingerprint standby never converges and refuses
    promotion."""
    case = Case('checkpoint-negotiation',
                'Foreign-model standby refuses checkpoint negotiation',
                'a third controller launched --standby <active> on the '
                'recipe-derived foreign-fingerprint document without '
                '--revised reports the named degraded negotiation '
                'failure for the observation window, POST /promote '
                'answers the named not_converged refusal, the active '
                'peer\'s ticks, field writes, and receipts continue '
                'undisturbed, and the case removes the foreign '
                'container afterward')
    start = ctx.get('start_foreign')
    stop = ctx.get('stop_foreign')
    foreign = ctx.get('foreign')
    if start is None or stop is None or foreign is None:
        return case.finish('inconclusive', 'the run context carries '
                           'no checkpoint-negotiation launch action, '
                           'teardown action, or foreign endpoint')
    try:
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        base = ctx[active]
        case.observe('field writer: ' + active + ' (' + base + ')')

        # The audit positions the refusal must leave untouched: the
        # pair's model fingerprint, the active's advancing tick, its
        # receipt log, and the field output it keeps writing.
        _, checkpoint = http_json('GET', base + '/checkpoint')
        pair_fp = checkpoint.get('model_fingerprint')
        if pair_fp is None:
            return case.finish('failed', 'the active peer serves no '
                               'model fingerprint')
        tick0 = (_try_snapshot(ctx, base) or {}).get('tick')
        _, body = http_json('GET', base + '/receipts')
        commands0 = [(r.get('command'), r.get('actor'))
                     for r in _receipt_list(body)]
        try:
            field_points = _field_out_points(ctx)
        except Exception as exc:
            return case.finish('inconclusive', 'the simulated plant '
                               'is unreachable: ' + str(exc)[:200])
        if not field_points:
            return case.finish('inconclusive', 'the simulated plant '
                               'serves no field output to watch')
        watch = min(field_points)
        ref = save_evidence(
            ctx['evidence_dir'], 'negotiation-before.json',
            {'active': active, 'model_fingerprint': pair_fp,
             'tick': tick0, 'receipts': len(commands0),
             'field': {str(p): _field_sample(ctx, p)
                       for p in field_points}})
        case.evidence('file', ref, 'the pre-launch audit positions')
        case.observe('baseline: fingerprint ' + str(pair_fp)
                     + ', tick ' + str(tick0) + ', '
                     + str(len(commands0)) + ' receipts, watching '
                     'field point ' + str(watch))

        # The runner-owned action: derive the foreign document through
        # the checked-in recipe and launch the third labeled controller
        # on it as --standby <active> without --revised, so its
        # fingerprint gate refuses every checkpoint it pulls.
        try:
            info = start(active)
        except Exception as exc:
            return case.finish('inconclusive', 'the checkpoint-'
                               'negotiation action never completed: '
                               + str(exc)[:300])
        container = str(info.get('container'))
        case.observe('foreign peer ' + container + ' launched on the '
                     'derived document without --revised')

        def attempt():
            """Everything the case asserts while the foreign peer is
            up — the refusal report, the observation window, the
            promote refusal, and the undisturbed-active checks."""
            document = json.loads(Path(info['document']).read_text())
            ref = save_evidence(ctx['evidence_dir'],
                                'negotiation-document.json', document)
            case.evidence('file', ref, 'the foreign-fingerprint model '
                          'document')

            # The named negotiation refusal: the foreign peer's pulls
            # land — the active serves — but every apply meets the
            # fingerprint gate, so the peer reports degraded naming the
            # mismatch and never converges. A converged report is an
            # outright contract violation; a peer still unsynchronized
            # or fetch-failing at the deadline never exercised the
            # negotiation — inconclusive.
            last = {}

            def verdict():
                role = _try_role(ctx, foreign)
                if role is None:
                    return None
                last['role'] = role
                sync = role.get('sync')
                if not isinstance(sync, dict):
                    return None
                if set(sync) & {'tracking', 'reinitialized',
                                'diverged'}:
                    return role
                detail = (sync.get('degraded') or {}).get('detail')
                if isinstance(detail, str) and 'fingerprint' in detail:
                    return role
                return None

            settled = wait_for(verdict,
                               time.monotonic() + NEGOTIATION_DEADLINE,
                               interval=NEGOTIATION_POLL)
            ref = save_evidence(ctx['evidence_dir'],
                                'negotiation-role.json',
                                last.get('role') or {})
            case.evidence('file', ref, 'the foreign peer\'s '
                          'negotiation state report')
            if settled is None:
                sync = (last.get('role') or {}).get('sync')
                detail = ((sync or {}).get('degraded') or {}) \
                    .get('detail') if isinstance(sync, dict) else None
                if isinstance(detail, str) and detail \
                        and 'fetch' not in detail:
                    return case.finish('failed', 'the foreign peer\'s '
                                       'refusal never named the '
                                       'fingerprint negotiation: '
                                       + detail[:300])
                return case.finish('inconclusive', 'the foreign peer '
                                   'never reached a negotiation '
                                   'verdict: ' + json.dumps(sync)[:300])
            sync = settled.get('sync') or {}
            if 'degraded' not in sync:
                return case.finish('failed', 'the foreign-fingerprint '
                                   'peer reports a converged state — '
                                   'the negotiation was not refused: '
                                   + json.dumps(sync)[:400])
            detail = str(sync['degraded'].get('detail'))
            case.observe('negotiation refused: ' + detail[:200])
            if isinstance(pair_fp, int) \
                    and format(pair_fp, '016x') not in detail:
                return case.finish('failed', 'the degraded report does '
                                   'not name the pair\'s fingerprint '
                                   + format(pair_fp, '016x') + ': '
                                   + detail[:300])

            # The observation window: the refusal must hold — every
            # poll keeps reporting standby+degraded — while the active
            # peer's tick keeps advancing and its field writes keep
            # landing.
            window = []
            last_tick = tick0
            field_tick = None
            mismatch = 0
            violation = None
            for _ in range(NEGOTIATION_ROUNDS):
                role = _try_role(ctx, foreign)
                snap = _try_snapshot(ctx, base)
                sample = _field_sample(ctx, watch)
                staged = _point_value(snap or {}, watch)
                tick = (snap or {}).get('tick')
                value = (sample or {}).get('value')
                if isinstance(value, dict):
                    value = next(iter(value.values()), None)
                ftick = (sample or {}).get('tick')
                window.append({'role': role, 'tick': tick,
                               'staged': staged, 'field': value,
                               'field_tick': ftick})
                sync = (role or {}).get('sync')
                if not (isinstance(role, dict)
                        and role.get('role') == 'standby'
                        and isinstance(sync, dict)
                        and 'degraded' in sync):
                    violation = ('the foreign peer left the refused '
                                 'state mid-window: '
                                 + json.dumps(role)[:300])
                if tick is not None and last_tick is not None \
                        and tick <= last_tick:
                    violation = ('the active peer\'s tick stalled at '
                                 + str(tick))
                if tick is not None:
                    last_tick = tick
                if ftick is not None and field_tick is not None \
                        and ftick <= field_tick:
                    violation = ('the field stopped receiving the '
                                 'active\'s writes at tick '
                                 + str(ftick))
                if ftick is not None:
                    field_tick = ftick
                if value is not None and staged is not None \
                        and value != staged:
                    mismatch += 1
                else:
                    mismatch = 0
                if violation:
                    break
                time.sleep(NEGOTIATION_POLL)
            ref = save_evidence(ctx['evidence_dir'],
                                'negotiation-window.json',
                                {'watch': watch, 'window': window})
            case.evidence('file', ref, 'the observation window: '
                          'foreign reports, active ticks, field reads')
            if violation:
                return case.finish('failed', violation)
            if mismatch >= 2:
                return case.finish('failed', 'the active peer\'s field '
                                   'writes stopped landing during the '
                                   'attempt: '
                                   + json.dumps(window[-3:])[:400])
            if not any(isinstance(w.get('role'), dict)
                       for w in window):
                return case.finish('inconclusive', 'the observation '
                                   'window saw no foreign report')
            case.observe('the refusal held across '
                         + str(len(window)) + ' polls; the active '
                         'peer\'s tick reached ' + str(last_tick))

            # The promotion gate: a never-converged peer must answer
            # the named not_converged refusal carrying its degraded
            # negotiation state — never a promotion.
            try:
                status, refusal = http_json('POST',
                                            foreign + '/promote')
            except urllib.error.HTTPError as exc:
                status = exc.code
                try:
                    refusal = json.loads(exc.read() or b'null')
                except ValueError:
                    refusal = None
                finally:
                    exc.close()
            ref = save_evidence(ctx['evidence_dir'],
                                'negotiation-refusal.json',
                                {'status': status, 'body': refusal})
            case.evidence('file', ref, 'POST /promote\'s answer')
            named = refusal.get('not_converged') \
                if isinstance(refusal, dict) else None
            if status != 409 or not isinstance(named, dict):
                return case.finish('failed', 'promote did not answer '
                                   'the named not_converged refusal: '
                                   'HTTP ' + str(status) + ' '
                                   + json.dumps(refusal)[:300])
            refused_sync = named.get('sync')
            if not isinstance(refused_sync, dict) \
                    or 'degraded' not in refused_sync:
                return case.finish('failed', 'the not_converged '
                                   'refusal does not carry the '
                                   'degraded negotiation state: '
                                   + json.dumps(refusal)[:300])
            case.observe('promote refused not_converged carrying '
                         + json.dumps(refused_sync)[:200])

            # The attempt touched nothing: the receipt log is exactly
            # the baseline's, the active's tick kept advancing, and its
            # writes still reach the field.
            _, body = http_json('GET', base + '/receipts')
            commands1 = [(r.get('command'), r.get('actor'))
                         for r in _receipt_list(body)]
            snap1 = _try_snapshot(ctx, base) or {}
            sample1 = _field_sample(ctx, watch)
            ref = save_evidence(ctx['evidence_dir'],
                                'negotiation-after.json',
                                {'tick': snap1.get('tick'),
                                 'receipts': len(commands1),
                                 'field': sample1})
            case.evidence('file', ref, 'the post-attempt audit '
                          'positions')
            if commands1 != commands0:
                return case.finish('failed', 'the active peer\'s '
                                   'receipt log changed across the '
                                   'attempt')
            if tick0 is not None and snap1.get('tick') is not None \
                    and snap1['tick'] <= tick0:
                return case.finish('failed', 'the active peer\'s tick '
                                   'did not advance through the '
                                   'attempt')
            if sample1 is None:
                return case.finish('inconclusive', 'the field stopped '
                                   'answering after the attempt')
            return case.finish('passed')

        try:
            record = attempt()
        except Exception as exc:
            record = case.finish('inconclusive', str(exc))
        # Teardown is unconditional once the peer is up: later cases —
        # the model-revision launch above all — need a clean rig. A
        # teardown that fails leaves that rig dirty, so a case that
        # otherwise passed cannot claim the contract held end to end.
        try:
            stop()
            case.observe('foreign peer ' + container + ' removed — '
                         'later cases see a clean rig')
        except Exception as exc:
            case.observe('the foreign peer teardown failed: '
                         + str(exc)[:200])
            if record['outcome'] == 'passed':
                record['outcome'] = 'inconclusive'
                record['detail'] = ('the foreign peer was never '
                                    'removed: ' + str(exc)[:300])
        return record
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
