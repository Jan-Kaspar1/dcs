"""The parameter_tune_carryover acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: The parameter-tune case also runs ahead of the failover leg: only
# ctrl-b tracks (its --standby source is ctrl-a), so a tuned value
# can cross a checkpoint only from ctrl-a to ctrl-b, and the
# promotion it performs is the run's one a->b switch — the failover
# leg behind it demotes whichever peer reports settled active and
# promotes the converged one back.
RUNS_BEFORE = frozenset({'scenario_failover'})


# --------------------------------------------------------------------
# The receipted parameter-tuning path and its carryover across
# promotion — WW-OPS-001's bounded, validated, receipted tuning clause
# and WW-LCM-001's runtime-tuning continuity. A descriptor-declared
# Float parameter out of the served registry is retuned through
# POST /command; the settled receipt, the served parameter report, and
# the transition journal must all reflect it; an out-of-range tune
# must meet the named out_of_range rejection and change nothing; and
# the promoted peer's parameter report must still carry the tuned
# value — the tuned state rode the checkpoint the tracking standby
# adopted rather than re-initializing to the model default.
#
# The case must run while ctrl-a is still the active: ctrl-b is the
# rig's only checkpoint-tracking peer (its --standby source is ctrl-a),
# so a tuned value can cross a checkpoint only from ctrl-a to ctrl-b.
# The switch it performs is also the run's one promotion — once ctrl-b
# claims the plant's single-writer claim, ctrl-a can never take the
# field back — so the case sits immediately ahead of the failover leg,
# which then re-cycles the surviving peer.

TUNE_DEADLINE = 30  # bound on the receipt, report, and switch waits


def _journal_covers_parameter(journal, component, name):
    """Whether a `GET /journal` payload recorded the scenario
    `set_parameter` command's settlement."""
    for entry in _journal_list(journal):
        receipt = entry.get('event', {}).get('command_settled', {}) \
            .get('receipt', {})
        tune = receipt.get('command', {}).get('set_parameter', {})
        if tune.get('component') == component \
                and tune.get('name') == name:
            return True
    return False


def scenario_parameter_tune_carryover(ctx):
    """A descriptor-declared Float parameter retuned through the
    receipted path survives the pair's promotion."""
    case = Case('parameter-tune-carryover',
                'Receipted parameter tune carries across promotion',
                'a descriptor-declared Float parameter retuned through '
                'POST /command settles applied, the served parameter '
                'report and the journal reflect it, an out-of-range '
                'tune meets the named out_of_range rejection and '
                'leaves the value unchanged, and the promoted peer\'s '
                'parameter report still carries the tuned value rather '
                'than the model-declared default')
    try:
        # In suite order the settled active is ctrl-a — the peer whose
        # checkpoints the tracking standby pulls — and the peer must
        # report tracking convergence for the promotion leg to carry
        # anything. A lone replay on a fresh rig finds the same layout.
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        peer = 'standby' if active == 'active' else 'active'
        base, peer_base = ctx[active], ctx[peer]
        case.observe('tuning against ' + active + ' (' + base + ')')

        def converged():
            try:
                report = _role(ctx, peer_base)
            except Exception:
                return None
            sync = report.get('sync') or {}
            return report if 'tracking' in sync else None

        tracking = wait_for(converged, time.monotonic() + TUNE_DEADLINE)
        ref = save_evidence(ctx['evidence_dir'],
                            'parameter-tune-roles.json',
                            {'peer': tracking})
        case.evidence('file', ref, 'the tracking peer\'s role report')
        if not tracking:
            return case.finish('inconclusive', 'the peer never '
                               'reported tracking convergence — the '
                               'promotion leg cannot be exercised')

        # The tune target: a descriptor-declared Float parameter whose
        # declared range holds a changed value and refuses a finite
        # out-of-range one, with its live value in the served report.
        _, schema = http_json('GET', base + '/schema')
        snapshot = _snapshot(ctx, base)
        ref = save_evidence(ctx['evidence_dir'],
                            'parameter-tune-interface.json',
                            {'schema': schema, 'snapshot': snapshot})
        case.evidence('file', ref, 'the served registry and live '
                      'parameter report the target is discovered from')
        plan = _float_tune_plan(schema, snapshot)
        if plan is None:
            return case.finish('inconclusive', 'no descriptor-declared '
                               'Float parameter with a violatable range '
                               'is served')
        component, name, current, tuned, outside = plan
        case.observe('tune target: ' + str(component) + ' '
                     + str(name) + ' ' + str(current) + ' -> '
                     + str(tuned) + ' (out-of-range probe '
                     + str(outside) + ')')

        # The receipted in-range tune: one submission, one receipt, the
        # settlement lands at the next scan boundary.
        command = {'command': {'set_parameter': {
            'component': component, 'name': name,
            'value': {'float': tuned}}}, 'actor': 'qa-lane'}
        index = _next_receipt_index(ctx, base)
        status, receipt = http_json('POST', base + '/command', command)
        ref = save_evidence(ctx['evidence_dir'],
                            'parameter-tune-submission.json',
                            {'command': command, 'status': status,
                             'receipt': receipt})
        case.evidence('file', ref, 'the in-range tune submission and '
                      'its receipt')
        if status != 200:
            return case.finish('failed', 'the in-range tune was '
                               'refused: ' + str(status) + ' '
                               + json.dumps(receipt)[:300])
        settled = wait_for(
            lambda: _settled_outcome(ctx, base, index),
            time.monotonic() + TUNE_DEADLINE)
        if settled != 'applied':
            return case.finish('failed', 'the in-range tune lacks a '
                               'settled receipt: '
                               + str(settled or 'never settled'))
        case.observe('tune receipt settled ' + settled)

        # The served parameter report reflects the standing tune — the
        # live read of the same fields the checkpoint captures.
        reflected = wait_for(
            lambda: (_parameter_value(s, component, name) == tuned
                     and s or None)
            if (s := _try_snapshot(ctx, base)) else None,
            time.monotonic() + TUNE_DEADLINE)
        report_snapshot = _try_snapshot(ctx, base) or {}
        ref = save_evidence(ctx['evidence_dir'],
                            'parameter-tune-report.json',
                            report_snapshot.get('parameters'))
        case.evidence('file', ref, 'the parameter report after the '
                      'tune settled')
        if not reflected:
            return case.finish('inconclusive', 'the served parameter '
                               'report never reflected the tune: '
                               + str(component) + ' ' + str(name)
                               + ' still reads '
                               + str(_parameter_value(report_snapshot,
                                                      component, name)))
        case.observe('the parameter report reads ' + str(tuned))

        # The run's audit: the settled tune's receipt is journaled.
        def journaled():
            try:
                _, journal = http_json('GET', base + '/journal')
            except Exception:
                return None
            journaled.last = journal
            return _journal_covers_parameter(journal, component, name) \
                or None

        journaled.last = []
        covered = wait_for(journaled, time.monotonic() + TUNE_DEADLINE)
        ref = save_evidence(ctx['evidence_dir'],
                            'parameter-tune-journal.json',
                            journaled.last)
        case.evidence('file', ref, 'the active\'s journal after the '
                      'tune settled')
        if not covered:
            return case.finish('failed', 'the journaled evidence '
                               'misses the receipted tune')

        # The out-of-range tune meets the named validation rejection at
        # submission — the descriptor range speaks before the queue —
        # and the standing tune is untouched.
        probe = {'command': {'set_parameter': {
            'component': component, 'name': name,
            'value': {'float': outside}}}, 'actor': 'qa-lane'}
        status, receipt = http_json('POST', base + '/command', probe)
        held = _parameter_value(_try_snapshot(ctx, base) or {},
                                component, name)
        ref = save_evidence(ctx['evidence_dir'],
                            'parameter-tune-rejection.json',
                            {'command': probe, 'status': status,
                             'receipt': receipt, 'reported': held})
        case.evidence('file', ref, 'the out-of-range submission, its '
                      'receipt, and the parameter report')
        outcome = _outcome_key(receipt if isinstance(receipt, dict)
                               else {})
        if status != 200 or outcome != 'rejected:out_of_range':
            return case.finish('failed', 'the out-of-range tune was '
                               'not rejected by name: ' + str(status)
                               + ' ' + json.dumps(receipt)[:300])
        if held != tuned:
            return case.finish('failed', 'the rejected out-of-range '
                               'tune changed the parameter: '
                               + str(held))
        case.observe('out-of-range tune rejected by name; '
                     + str(name) + ' still reads ' + str(held))

        # The tuned value must reach the tracking peer through the
        # checkpoint stream before the switch — the carryover the
        # promotion is about to prove.
        carried = wait_for(
            lambda: (_parameter_value(s, component, name) == tuned
                     and s or None)
            if (s := _try_snapshot(ctx, peer_base)) else None,
            time.monotonic() + TUNE_DEADLINE)
        standby_snapshot = _try_snapshot(ctx, peer_base) or {}
        ref = save_evidence(ctx['evidence_dir'],
                            'parameter-tune-standby.json',
                            standby_snapshot.get('parameters'))
        case.evidence('file', ref, 'the tracking peer\'s parameter '
                      'report before the switch')
        if not carried:
            return case.finish('inconclusive', 'the tracking peer\'s '
                               'parameter report never reflected the '
                               'tune — the checkpoint carryover cannot '
                               'be observed')

        # The switch: demote the tuned active, promote the converged
        # standby, and read the promoted peer's own parameter report.
        status, body = http_json('POST', base + '/demote')
        case.observe('demote ' + active + ': ' + str(status) + ' '
                     + json.dumps(body))
        if status != 200:
            return case.finish('failed', 'demote refused: '
                               + str(body))
        promoted = None
        deadline = time.monotonic() + TUNE_DEADLINE
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
            time.monotonic() + TUNE_DEADLINE)
        ref = save_evidence(ctx['evidence_dir'],
                            'parameter-tune-promotion.json',
                            {'demoted': active, 'promote': promoted,
                             'role': settled_role})
        case.evidence('file', ref, 'the demote/promote responses and '
                      'the promoted peer\'s role')
        if promoted is None:
            return case.finish('failed', 'the converged standby never '
                               'promoted within ' + str(TUNE_DEADLINE)
                               + 's')
        if not settled_role:
            return case.finish('failed', 'the promoted peer did not '
                               'settle active')

        promoted_report = wait_for(
            lambda: (_parameter_value(s, component, name) == tuned
                     and s or None)
            if (s := _try_snapshot(ctx, peer_base)) else None,
            time.monotonic() + TUNE_DEADLINE)
        last = _try_snapshot(ctx, peer_base)
        if last is None:
            return case.finish('inconclusive', 'the promoted peer\'s '
                               'monitor never served a parameter '
                               'report')
        ref = save_evidence(ctx['evidence_dir'],
                            'parameter-tune-promoted.json',
                            last.get('parameters'))
        case.evidence('file', ref, 'the promoted peer\'s parameter '
                      'report')
        if not promoted_report:
            found = _parameter_value(last, component, name)
            if found == current:
                return case.finish('failed', 'the promoted peer '
                                   'reverted ' + str(name) + ' to the '
                                   'model-declared default '
                                   + str(current) + ' — the tune did '
                                   'not ride the checkpoint')
            return case.finish('failed', 'the promoted peer lost the '
                               'tuned value: ' + str(name)
                               + ' reads ' + str(found))
        case.observe('the promoted peer still reports ' + str(name)
                     + ' = ' + str(tuned) + ' — the tune crossed the '
                     'checkpoint')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
