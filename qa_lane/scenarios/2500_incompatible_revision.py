"""The incompatible_revision acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: The incompatible-revision case sits immediately ahead of it: its
# carryover-breaking peer never promotes, so the field writer is
# unchanged, and the compatible case's launch replaces the degraded
# third container and performs the control's promote leg in the same
# run.
RUNS_AFTER = frozenset({'scenario_checkpoint_negotiation', 'scenario_doomed_startup_claim', 'scenario_failover'})
RUNS_BEFORE = frozenset({'scenario_model_revision'})


# The rolling-revision refusal half: WW-LCM-001's deployment-update
# clause requires the carryover rule to refuse a revision that retypes
# a carried point — never silently loading the incompatible document.
# The case derives the recipe's revised document exactly as the
# compatible roll does, then applies the checked-in post-derivation
# step (qa_lane/revision-incompatible.json) that retypes one carried
# writable internal point bool->int, so every pulled checkpoint fails
# with the named InternalKindMismatch and the peer settles degraded —
# distinguishably from the foreign-fingerprint degrade an unarmed
# standby reports — refuses POST /promote with the not_converged
# SwitchError carrying the same detail, and leaves the field writer
# undisturbed. The control half relaunches the same slot on the same
# document minus the retype and must converge reinitialized — proving
# the refusal names the carryover violation rather than a rig defect.
# The scenario sits immediately ahead of scenario_model_revision in
# the schedule: it never promotes, so the field writer is unchanged
# and the compatible case behind it relaunches the third slot and
# performs the control's promote leg in the same run.


def scenario_incompatible_revision(ctx):
    """A --revised peer on a carryover-breaking document is refused."""
    case = Case('incompatible-revision',
                'Incompatible model revision meets the named refusal',
                'a third controller launched --standby <active> '
                '--revised on the recipe-derived document plus the '
                'checked-in incompatible retype settles standby '
                'reporting sync degraded with the detail naming the '
                'carryover refusal and the retyped point — not the '
                'foreign-fingerprint degrade — POST /promote answers '
                '409 not_converged carrying the same degraded detail, '
                'the active\'s field writes, receipts, and journal '
                'stay undisturbed across the observation window, and '
                'the same document minus the retype converges '
                'reinitialized as the control half')
    try:
        start = ctx.get('start_revised')
        revised = ctx.get('revised')
        if start is None or revised is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no model-revision action or '
                               'revised endpoint')
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        if active not in ('active', 'standby'):
            return case.finish('inconclusive', 'the field writer is '
                               'already the revised peer — the '
                               'incompatible variant has no pair '
                               'member to stand by on')
        base = ctx[active]
        case.observe('field writer: ' + active + ' (' + base + ')')

        # The audit positions the refusal must leave untouched: the
        # active's receipt log, its durable journal file, and one
        # field output its scan keeps writing.
        _, body = http_json('GET', base + '/receipts')
        receipts0 = _receipt_list(body)
        commands0 = [(r.get('command'), r.get('actor'))
                     for r in receipts0]
        journals = ctx.get('journal_files') or {}
        journal_path = journals.get(active)
        journal0 = None
        if journal_path:
            try:
                journal0 = _journal_entries(journal_path)
            except (OSError, ValueError) as exc:
                return case.finish('inconclusive', 'the active\'s '
                                   'journal file is unreadable: '
                                   + str(exc))
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
            ctx['evidence_dir'], 'incompatible-revision-before.json',
            {'active': active, 'receipts': len(receipts0),
             'journal_records': len(journal0 or []),
             'field': {str(p): _field_sample(ctx, p)
                       for p in field_points}})
        case.evidence('file', ref, 'the pre-refusal audit positions')

        # The runner-owned action on the refusal half: the additive
        # recipe derives the revised document first, then the
        # checked-in step retypes a carried point so the carryover
        # crossing — not the document's load — is what fails.
        try:
            info = start(active, incompatible=True)
        except Exception as exc:
            return case.finish('inconclusive', 'the incompatible '
                               'model-revision action never '
                               'completed: ' + str(exc)[:300])
        retyped = info.get('retyped_point')
        if not isinstance(retyped, int):
            return case.finish('inconclusive', 'the incompatible '
                               'derivation did not name the retyped '
                               'point: ' + json.dumps(info)[:200])
        case.observe('revised peer ' + str(info.get('container'))
                     + ' launched on the incompatible document — '
                     'retyped point ' + str(retyped))
        document = json.loads(Path(info['document']).read_text())
        ref = save_evidence(ctx['evidence_dir'],
                            'incompatible-revision-document.json',
                            document)
        case.evidence('file', ref, 'the incompatible derived model '
                      'document')

        # The refusal: every pulled checkpoint crosses into the
        # carryover rule and fails it, so the peer settles degraded
        # permanently — not the transient degrade of a fetch failure —
        # with the detail naming the carryover error and the retyped
        # point. A `reinitialized` report here would mean the
        # incompatible document silently crossed: the contract break
        # this case exists to catch.
        last = {}

        def refusal_report():
            role = _try_role(ctx, revised)
            if role is None:
                return None
            last['role'] = role
            sync = role.get('sync')
            if not isinstance(sync, dict):
                return None
            if 'reinitialized' in sync:
                return role
            detail = str((sync.get('degraded') or {}).get('detail'))
            if 'internal point ' + str(retyped) in detail \
                    and 'retype must rename' in detail:
                return role
            return None

        settled = wait_for(refusal_report,
                           time.monotonic() + REVISION_CONVERGE_DEADLINE,
                           interval=REVISION_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'incompatible-revision-role.json',
                            last.get('role') or {})
        case.evidence('file', ref, 'the incompatible peer\'s role '
                      'report')
        sync = ((settled or last.get('role') or {}).get('sync'))
        if isinstance(sync, dict) and 'reinitialized' in sync:
            return case.finish('failed', 'the incompatible document '
                               'converged reinitialized — the '
                               'carryover rule did not refuse the '
                               'retype of point ' + str(retyped))
        if settled is None:
            if isinstance(sync, dict) and 'degraded' in sync:
                return case.finish('failed', 'the peer degraded but '
                                   'not on the named carryover '
                                   'refusal: ' + json.dumps(sync)[:300])
            return case.finish('inconclusive', 'the incompatible '
                               'peer never reported a checkpoint '
                               'crossing: ' + json.dumps(sync)[:300])
        detail = str((sync.get('degraded') or {}).get('detail'))
        if 'fingerprint' in detail:
            return case.finish('failed', 'the degrade is a '
                               'fingerprint rejection, not the '
                               'carryover refusal: ' + detail[:300])
        if settled.get('role') != 'standby':
            return case.finish('failed', 'the refused peer reports '
                               'role ' + str(settled.get('role')))
        case.observe('degraded on the named refusal: ' + detail)

        # The named refusal on the switch path: POST /promote meets
        # the degraded peer's convergence gate — HTTP 409 carrying the
        # not_converged SwitchError whose embedded sync repeats the
        # carryover detail.
        try:
            status, refusal = http_json('POST', revised + '/promote')
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                refusal = json.loads(exc.read() or b'null')
            except ValueError:
                refusal = None
            finally:
                exc.close()
        ref = save_evidence(ctx['evidence_dir'],
                            'incompatible-revision-refusal.json',
                            {'status': status, 'body': refusal})
        case.evidence('file', ref, 'the refused promotion')
        if status != 409:
            return case.finish('failed', 'POST /promote answered '
                               + str(status) + ' — the degraded peer '
                               'must refuse with 409: '
                               + json.dumps(refusal)[:300])
        refusal_sync = (((refusal or {}).get('not_converged') or {})
                        .get('sync'))
        refusal_detail = str((refusal_sync.get('degraded') or {})
                             .get('detail')) \
            if isinstance(refusal_sync, dict) else ''
        if 'internal point ' + str(retyped) not in refusal_detail \
                or 'retype must rename' not in refusal_detail:
            return case.finish('failed', 'the promotion refusal does '
                               'not carry the named carryover '
                               'failure: ' + json.dumps(refusal)[:400])
        case.observe('promotion refused: ' + refusal_detail)

        # The undisturbed active: across the observation window the
        # field keeps following the active's staged image (one scan of
        # lag allowed), the receipt log keeps its exact contents, and
        # the durable journal file records nothing new — the refused
        # peer never wrote, never promoted, never touched the run's
        # audit trail.
        trace = []
        consecutive = 0
        for _ in range(REVISION_FIELD_ROUNDS):
            sample = _field_sample(ctx, watch)
            staged = _point_value(_try_snapshot(ctx, base) or {},
                                  watch)
            value = (sample or {}).get('value')
            if isinstance(value, dict):
                value = next(iter(value.values()), None)
            trace.append({'field': value, 'active_staged': staged})
            if value is not None and staged is not None \
                    and value != staged:
                consecutive += 1
            else:
                consecutive = 0
            time.sleep(REVISION_POLL)
        _, body = http_json('GET', base + '/receipts')
        receipts1 = _receipt_list(body)
        commands1 = [(r.get('command'), r.get('actor'))
                     for r in receipts1]
        journal1 = None
        journal_error = None
        if journal_path:
            try:
                journal1 = _journal_entries(journal_path)
            except (OSError, ValueError) as exc:
                journal_error = str(exc)
        ref = save_evidence(
            ctx['evidence_dir'], 'incompatible-revision-field.json',
            {'watch': watch, 'trace': trace,
             'receipts': [len(receipts0), len(receipts1)],
             'journal_records': [len(journal0 or []),
                                 None if journal1 is None
                                 else len(journal1)]})
        case.evidence('file', ref, 'the active across the refusal '
                      'window')
        if consecutive >= 2:
            return case.finish('failed', 'the field stopped following '
                               'the still-active peer\'s image during '
                               'the refusal: '
                               + json.dumps(trace[-3:])[:400])
        if commands1 != commands0:
            return case.finish('failed', 'the active\'s receipt log '
                               'changed across the refusal window: '
                               + str(len(receipts0)) + ' -> '
                               + str(len(receipts1)))
        if journal_error:
            return case.finish('inconclusive', 'the active\'s '
                               'journal file turned unreadable: '
                               + journal_error)
        if journal0 is not None and journal1 != journal0:
            return case.finish('failed', 'the active\'s journal file '
                               'gained records across the refusal '
                               'window: ' + str(len(journal0)) + ' -> '
                               + str(len(journal1)))

        # The control half: the same recipe-derived document minus the
        # retype must converge reinitialized — the refusal above named
        # the carryover violation, not a rig defect. The relaunch also
        # exercises the action's third-slot replacement over the
        # degraded peer. The promote leg belongs to the sibling
        # model-revision case scheduled immediately behind, which
        # relaunches the slot and performs the demote-then-promote.
        try:
            control = start(active)
        except Exception as exc:
            return case.finish('inconclusive', 'the control '
                               'relaunch never completed: '
                               + str(exc)[:300])
        document = json.loads(Path(control['document']).read_text())
        ref = save_evidence(ctx['evidence_dir'],
                            'incompatible-revision-control.json',
                            document)
        case.evidence('file', ref, 'the control document — the same '
                      'revision minus the retype')
        last.clear()

        def control_converged():
            role = _try_role(ctx, revised)
            if role is None:
                return None
            last['role'] = role
            sync = role.get('sync')
            if isinstance(sync, dict) and 'reinitialized' in sync:
                return role
            return None

        converged = wait_for(control_converged,
                             time.monotonic()
                             + REVISION_CONVERGE_DEADLINE,
                             interval=REVISION_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'incompatible-revision-control-role.json',
                            last.get('role') or {})
        case.evidence('file', ref, 'the control peer\'s convergence')
        if converged is None:
            sync = (last.get('role') or {}).get('sync')
            return case.finish('failed', 'the control half did not '
                               'converge reinitialized — the refusal '
                               'may name a rig defect rather than the '
                               'retype: ' + json.dumps(sync)[:300])
        report = ((converged.get('sync') or {})
                  .get('reinitialized') or {}).get('report') or {}
        carried = report.get('carried') or []
        if not any(c.get('point') == retyped
                   and isinstance(c.get('value'), dict)
                   and set(c['value']) == {'bool'}
                   for c in carried):
            return case.finish('failed', 'the control crossing did '
                               'not carry the retyped point as its '
                               'old kind: carried '
                               + json.dumps(carried)[:300])
        case.observe('control half converged reinitialized — point '
                     + str(retyped) + ' carried under its declared '
                     'bool kind')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
