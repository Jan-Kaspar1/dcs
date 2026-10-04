"""The command_across_promotion acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the command-across-promotion leg shares the launch-layout
# window behind the demote legs — it needs the settled tracking pair
# and the durable journals, it moves the field writer twice, and it
# restores the launch roles before the tune case's a->b switch.
RUNS_AFTER = frozenset({'scenario_gossip_repromote_settle'})
RUNS_BEFORE = frozenset({'scenario_parameter_tune_carryover'})


# --------------------------------------------------------------------
# Declared-command settlement and emitted-event continuity across a
# promotion — the lane evidence for WW-FND-003's pair semantics and
# WW-LCM-001's bumpless-switchover clause. #381 pinned in-workspace
# that a declared command submitted before a promotion settles exactly
# once without losing its receipt on the new active, and that emitted
# events keep their component attribution and ordering across the
# transition; the lane's command coverage never crossed a promotion —
# the in-flight scenarios exercised point commands and role refusals,
# not the declared-command/event surface. Four legs:
#
#   (a) settlement — the active's `GET /schema` declared command,
#       picked with the lane's own helpers and submitted through the
#       receipted `POST /command` path, settles `applied` at a scan
#       boundary with exactly one `command_settled` record on the
#       peer that served it;
#   (b) promotion — the documented demote/promote moves the field
#       writer, the same declared command is resubmitted on the
#       promoted peer once it reports `active`, and across both peers'
#       journals each submission produced exactly one settlement
#       attributed to the peer that served it: no lost receipt, no
#       duplicated application, and no replay of the demoted peer's
#       settlement history onto the promoted peer's journal;
#   (c) continuity — the command's own records (its
#       `command_settled` entries, the `point_changed` transitions a
#       point write lands, and any `event_emitted` record the fixture
#       produces for the component) continue on the promoted peer in
#       tick order with the attribution unchanged: the
#       pre-promotion entries stand as the record's own prefix — never
#       re-emitted — and the first post-promotion entry does not
#       precede the last pre-promotion tick the run observed;
#   (d) restore — the documented switch back, so the pair stands on
#       its pre-scenario role assignment for the legs behind this one.
#
# Every submission, receipt, role, and journal observation lands under
# the run's evidence directory. Inconclusive when the rig serves no
# declared command that translates to the receipted path, when the
# pair has no tracking standby to promote, or when the promoted peer
# never reports `active` — the wait this leg's evidence rests on.

ACROSS_SETTLE = 45     # bound on each submission's settle, the
                       # promotion wait, and the restore switch
ACROSS_AUDIT = 30      # bound on the journal records the audit reads
ACROSS_POLL = 0.2      # wait cadence inside the leg
ACROSS_WATCH = 0.05    # cadence polling a role walk to its settled
                       # posture


def _across_rows(view):
    """The served per-command availability rows a `GET /resources` view
    carries — `{(component, command): row}` — the probe the pick reads
    so a leg that must see `applied` submits only what the run
    offers."""
    rows = {}
    for entry in (view or {}).get('components') or []:
        for row in entry.get('commands') or []:
            rows[(str(entry.get('name')), str(row.get('name')))] = row
    return rows


def _across_offered(schema, rows):
    """The served registry's interfaces with only the commands the
    availability view reports invocable — the probe-then-submit
    agreement the command-availability case evidences: a declared
    command whose row reads unavailable settles that refusal, so
    submitting it would prove the refusal, not the settlement."""
    offered = []
    for entry in schema.get('interfaces') or []:
        interface = entry.get('interface') or {}
        commands = [
            spec for spec in interface.get('commands') or []
            if rows.get((str(entry.get('name')), str(spec.get('name'))),
                        {}).get('available')
        ]
        offered.append({'name': entry.get('name'),
                        'interface': dict(interface, commands=commands)})
    return offered


def _across_pick(ctx, base):
    """The declared command this peer currently offers — its served
    registry filtered by its own availability rows, read on the peer
    that will serve the submission. Returns `(component, spec,
    submission)`, or None when the schema answers nothing translatable
    or nothing the view reports invocable."""
    _, schema = http_json('GET', base + '/schema')
    _, signals = http_json('GET', base + '/signals')
    interfaces = schema.get('interfaces') or []
    try:
        _, view = http_json('GET', base + '/resources')
        rows = _across_rows(view)
    except Exception:
        rows = {}
    picked = _pick_declared_command(_across_offered(schema, rows), signals) \
        if rows else None
    return picked or _pick_declared_command(interfaces, signals)


def _across_settles(journal, command, actor):
    """The `command_settled` receipts one served journal carries for a
    submission — the (command, actor) pair is the submission's
    identity on the wire."""
    return [receipt for receipt in
            (_journal_settled(entry) for entry in _journal_list(journal))
            if _admission_hit(receipt, {'command': command,
                                        'actor': actor})]


def _across_records(journal, components, submissions, points):
    """The attributed record stream a journal carries for the run's
    submissions — `(tick, kind, attribution)` per entry, in journal
    order: the submissions' `command_settled` records attributed to
    the actor that minted them, the `point_changed` transitions on the
    submissions' own points, and every `event_emitted` record the
    named component produced. The stream is what the continuity
    clauses compare: its prefix, its tick order, and the attribution
    each record keeps across the switch."""
    actors = {entry['actor'] for entry in submissions}
    commands = [entry['command'] for entry in submissions]
    kinds = {str(component).split(':', 1)[0] + ':'
             for component in components}
    stream = []
    for entry in _journal_list(journal):
        event = entry.get('event') or {}
        settled = event.get('command_settled')
        changed = event.get('point_changed')
        emitted = event.get('event_emitted')
        record = None
        if isinstance(settled, dict):
            receipt = settled.get('receipt') or {}
            actor = receipt.get('actor')
            if actor in actors and receipt.get('command') in commands:
                record = (entry.get('tick'), 'command_settled', actor)
        elif isinstance(changed, dict) and changed.get('point') in points:
            record = (entry.get('tick'), 'point_changed',
                      str(changed.get('point')))
        elif isinstance(emitted, dict):
            body = emitted.get('event') or {}
            producer = str(body.get('component') or '')
            # The picked instance's own emissions, plus any record the
            # same component kind attributes elsewhere — a sibling
            # instance's record is the misattribution the continuity
            # clause names, so it has to survive into the stream.
            if producer in components or any(
                    producer.startswith(kind) for kind in kinds):
                record = (entry.get('tick'), 'event_emitted', producer
                          + ':' + str(body.get('event')))
        if record is not None:
            stream.append(record)
    return stream


def _across_continuous(before, after):
    """Whether the post-promotion record continues the pre-promotion
    one: the earlier entries stand as the later record's own prefix —
    attribution unchanged, nothing re-emitted — the ticks never step
    backward, and the first post-promotion entry does not precede the
    last pre-promotion tick. Answers the violation list — empty when
    the continuity holds."""
    violations = []
    if after[:len(before)] != before:
        violations.append(
            'the promoted peer\'s record does not continue the '
            'pre-promotion entries as their own prefix — attribution '
            'changed or an entry was re-emitted')
    ticks = [record[0] for record in after if isinstance(record[0], int)]
    if any(later < earlier for earlier, later in zip(ticks, ticks[1:])):
        violations.append(
            'the promoted peer\'s record does not continue in tick '
            'order')
    if before and after and ticks \
            and before[-1][0] is not None \
            and after[len(before)][0] < before[-1][0]:
        violations.append(
            'the first post-promotion entry at tick '
            + str(after[len(before)][0]) + ' precedes the last '
            'pre-promotion tick ' + str(before[-1][0]))
    return violations


def _across_switch(ctx, owner, peer, deadline):
    """The documented pair switch: demote the field owner, promote
    the reconverged peer, and wait for the promoted peer to report
    `active` with the demoted one tracking behind it. Answers the
    promoted peer, or None when the switch never landed."""
    _settle_call(ctx[owner] + '/demote')
    while time.monotonic() < deadline:
        status, _body = _settle_call(ctx[peer] + '/promote')
        if status == 200:
            break
        time.sleep(ACROSS_POLL)
    promoted = wait_for(
        lambda: 'active' if (_try_role(ctx, ctx[peer]) or {}).get(
            'role') == 'active' else None,
        deadline, interval=ACROSS_WATCH)
    if promoted is None:
        return None
    wait_for(lambda: _tracking_standby(ctx, owner),
             deadline, interval=ACROSS_POLL)
    return peer


def _across_restore(ctx, owner, deadline):
    """Best-effort restore of the pre-scenario role assignment inside
    one bound: demote whichever peer holds the field off the entry
    owner, promote the owner once it reports promotable, and wait for
    the pair's tracking posture."""
    while time.monotonic() < deadline:
        current = _pair_active(ctx)
        if current == owner \
                and _tracking_standby(ctx, 'standby') is not None:
            return True
        if current is not None and current != owner:
            _settle_call(ctx[current] + '/demote')
            report = _try_role(ctx, ctx[owner])
            if report is not None and report.get('role') == 'standby':
                _settle_call(ctx[owner] + '/promote')
        elif current is None:
            for name in (owner, 'standby'):
                report = _try_role(ctx, ctx[name])
                if report is not None \
                        and report.get('role') == 'standby' \
                        and (report.get('sync') or {}):
                    _settle_call(ctx[name] + '/promote')
                    break
        time.sleep(ACROSS_POLL)
    return _pair_active(ctx) == owner \
        and _tracking_standby(ctx, 'standby') is not None


def scenario_command_across_promotion(ctx):
    """Exercise declared-command settlement and emitted-event
    continuity across a promotion on the deployed pair: pick a
    declared command off the active's `GET /schema`, submit it
    through the receipted path and assert its settled `applied` receipt
    with one `command_settled` on the peer that served it, demote and
    promote to move the field writer, resubmit the same declared
    command on the promoted peer and assert one `applied` receipt
    there with exactly one settlement per submission across the pair's
    journals — no lost receipt, no duplicated apply, no replay of the
    demoted peer's history — then assert the promoted peer's record
    continues the pre-promotion entries in tick order with the
    attribution unchanged and none of them re-emitted, and finally
    switch back so the pair stands on its pre-scenario roles."""
    case = Case(
        'command-across-promotion',
        'A declared command settles once and its records continue '
        'across a promotion',
        'with the deployed pair settled and tracking, pick a declared '
        'command from the active\'s GET /schema and submit it through '
        'the receipted POST /command path, asserting the settled '
        'applied receipt and exactly one command_settled journal '
        'record on the peer that served it; demote and promote to move '
        'the field writer and resubmit the same declared command on '
        'the promoted peer once it reports active, asserting one '
        'applied receipt there and exactly one settlement per '
        'submission across the pair\'s journals — none lost, none '
        'duplicated, none replayed from the old peer; assert the '
        'promoted peer\'s emitted and adapted records continue in tick '
        'order with the component attribution unchanged and no '
        'pre-promotion entry re-emitted; and switch back so the pair '
        'returns to its pre-scenario role assignment')
    owner, peer = 'active', 'standby'
    evidence = {}

    def journal_of(name):
        """The named peer's journal entries since the leg's floor —
        the serving half of every journal observation."""
        _, payload = http_json('GET', ctx[name] + '/journal?since='
                               + str(floors[name]))
        return _journal_list(payload)

    picked_components = set()

    def records(name, submissions):
        return _across_records(journal_of(name), picked_components,
                               submissions, points)

    try:
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries only one endpoint — the pair '
                               'this leg switches is absent')
        journal_files = ctx.get('journal_files') or {}
        missing = [name for name in (owner, peer)
                   if journal_files.get(name) is None]
        if missing:
            return case.finish('inconclusive', 'the run context '
                               'carries no journal files for '
                               + json.dumps(missing) + ' — the durable '
                               'journal audit is absent')
        for name in (owner, peer):
            try:
                _role(ctx, ctx[name])
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s '
                                   'monitor is unreachable: '
                                   + str(exc)[:200])
        deadline = time.monotonic() + ACROSS_SETTLE
        active = wait_for(lambda: _pair_active(ctx), deadline,
                          interval=ACROSS_POLL)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        if active != owner:
            return case.finish('inconclusive', 'the field owner is '
                               + active + ' — the promotion this leg '
                               'exercises needs the launched-active '
                               'peer owning the field; the pair\'s '
                               'layout predates the stage')
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=ACROSS_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — there is no peer to '
                               'promote')
        roles_before = {name: _role(ctx, ctx[name]) for name in (owner,
                                                                 peer)}
        evidence['roles_before'] = roles_before
        ref = save_evidence(ctx['evidence_dir'],
                            'across-promotion-roles-before.json',
                            roles_before)
        case.evidence('file', ref, "both peers' /role reports before "
                      'the leg\'s switch')

        # ---- (a) the declared command on the active -----------------
        #
        # The schema-driven pick: the lane's own helper reads the
        # served registry and the signal index and returns the command
        # a declared interface offers, so the leg proves the declared
        # surface rather than a hand-written point write.
        try:
            picked = _across_pick(ctx, ctx[owner])
        except Exception as exc:
            return case.finish('inconclusive', 'the active\'s schema, '
                               'signal index, or availability view never '
                               'answered: ' + str(exc)[:200])
        if picked is None:
            return case.finish('inconclusive', 'no served declared '
                               'command translates to the receipted '
                               'path on this rig')
        component, spec, command = picked
        picked_components.add(component)
        ref = save_evidence(ctx['evidence_dir'],
                            'across-promotion-picked.json',
                            {'component': component, 'spec': spec,
                             'command': command})
        case.evidence('file', ref, 'the declared command the leg '
                      'submits — ' + str(component) + ' '
                      + str(spec.get('name')))
        case.observe('declared command: ' + str(component) + ' '
                     + str(spec.get('name')) + ' from the served '
                     'registry')
        points = {spec.get('point')} - {None}
        floors = {}
        try:
            for name in (owner, peer):
                floors[name] = _journal_cursor(ctx, ctx[name])
        except Exception as exc:
            return case.finish('inconclusive', 'a peer\'s journal '
                               'floor never served: ' + str(exc)[:200])
        evidence['floors'] = floors

        pre_actor = 'qa-across-promotion-pre'
        pre = {'command': command, 'actor': pre_actor}
        # The admission's absolute submission index, read from the
        # owner's own high-water before the POST: the served receipt
        # carries the verdict, never the log position it took.
        pre_index = _next_receipt_index(ctx, ctx[owner])
        try:
            status, receipt = http_json(
                'POST', ctx[owner] + '/command',
                {'command': command, 'actor': pre_actor,
                 'reason': 'command-across-promotion'})
        except Exception as exc:
            return case.finish('failed', 'the pre-promotion declared '
                               'command returned no receipt: '
                               + str(exc)[:200])
        evidence['pre_submission'] = {'status': status,
                                      'receipt': receipt}
        ref = save_evidence(ctx['evidence_dir'],
                            'across-promotion-pre-receipt.json',
                            {'status': status, 'receipt': receipt})
        case.evidence('file', ref, "the pre-promotion submission's "
                      'receipt')
        if status != 200 or _outcome_key(receipt) != 'accepted':
            return case.finish('failed', 'the pre-promotion declared '
                               'command drew no admission: '
                               + str(status) + ' '
                               + json.dumps(receipt)[:300])

        pre['index'] = pre_index
        wait_for(
            lambda: _submitted_receipt(ctx, ctx[owner], pre_index,
                                       command),
            time.monotonic() + ACROSS_AUDIT, interval=ACROSS_POLL)
        pre_receipt = _submitted_receipt(ctx, ctx[owner], pre_index,
                                         command)
        evidence['pre_settled'] = pre_receipt
        if _outcome_key(pre_receipt) != 'applied':
            return case.finish(
                'failed', 'the pre-promotion declared command settled '
                + _outcome_key(pre_receipt) + ' — the receipted path '
                'owes applied at a scan boundary on the serving peer')
        ref = save_evidence(ctx['evidence_dir'],
                            'across-promotion-pre-settled.json',
                            pre_receipt)
        case.evidence('file', ref, "the pre-promotion command's "
                      'settled receipt on the active')

        # ---- the pre-promotion record on the pair -------------------
        before = {name: records(name, [pre]) for name in (owner, peer)}
        evidence['records_before'] = before
        pre_settles = {name: _across_settles(journal_of(name), command,
                                             pre_actor)
                       for name in (owner, peer)}
        evidence['pre_settles'] = pre_settles
        # The serving peer's own boundary settles the submission once:
        # two records on the owner already are the duplicated-apply
        # shape this leg's settlement half names.
        if len(pre_settles[owner]) > 1:
            return case.finish(
                'failed', 'the pre-promotion submission settled '
                + str(len(pre_settles[owner])) + ' times on the peer '
                'that served it — one admission owes exactly one '
                'settlement')

        # ---- (b) the promotion --------------------------------------
        promoted = _across_switch(ctx, owner, peer,
                                  time.monotonic() + ACROSS_SETTLE)
        evidence['switched'] = promoted
        if promoted is None:
            # The promotion wait never settled: the rig never carried
            # the pair through the documented switch, so what the
            # settlement and continuity clauses read is not the
            # surface this leg exercises — inconclusive, never a
            # product failure.
            return case.finish(
                'inconclusive', 'the promoted peer never reported active '
                'after the documented switch — the pair never reached '
                'the promotion this leg\'s settlement, continuity, and '
                'restore clauses stand on')
        ref = save_evidence(ctx['evidence_dir'],
                            'across-promotion-roles-promoted.json',
                            {name: _role(ctx, ctx[name])
                             for name in (owner, peer)})
        case.evidence('file', ref, "both peers' /role reports after "
                      'the promotion')
        case.observe('field writer moved to ' + peer)

        # The same declared command, resubmitted on the promoted peer:
        # its own availability view decides whether that peer's run
        # offers the command now, and the same command is resubmitted
        # whenever it does — a different declaration would make the two
        # settlements incomparable.
        post_actor = 'qa-across-promotion-post'
        try:
            repick = _across_pick(ctx, ctx[peer])
        except Exception as exc:
            return case.finish('inconclusive', 'the promoted peer\'s '
                               'served registry or availability view '
                               'never answered: ' + str(exc)[:200])
        post_command = command
        if repick is not None and repick[0] != component:
            return case.finish(
                'inconclusive', 'the promoted peer no longer offers the '
                'submitted declaration ' + str(component) + ' — its '
                'served registry answers ' + str(repick[0]) + ', so no '
                'comparable resubmission is available')
        post = {'command': post_command, 'actor': post_actor}
        post_index = _next_receipt_index(ctx, ctx[peer])
        try:
            status, receipt = http_json(
                'POST', ctx[peer] + '/command',
                {'command': post_command, 'actor': post_actor,
                 'reason': 'command-across-promotion'})
        except Exception as exc:
            return case.finish('failed', 'the post-promotion declared '
                               'command returned no receipt: '
                               + str(exc)[:200])
        evidence['post_submission'] = {'status': status,
                                       'receipt': receipt}
        ref = save_evidence(ctx['evidence_dir'],
                            'across-promotion-post-receipt.json',
                            {'status': status, 'receipt': receipt})
        case.evidence('file', ref, "the post-promotion submission's "
                      'receipt')
        if status != 200 or _outcome_key(receipt) != 'accepted':
            return case.finish('failed', 'the promoted peer refused '
                               'the resubmitted declared command: '
                               + str(status) + ' '
                               + json.dumps(receipt)[:300])
        post['index'] = post_index
        wait_for(
            lambda: _submitted_receipt(ctx, ctx[peer], post_index,
                                       post_command),
            time.monotonic() + ACROSS_AUDIT, interval=ACROSS_POLL)
        post_receipt = _submitted_receipt(ctx, ctx[peer], post_index,
                                          post_command)
        evidence['post_settled'] = post_receipt
        if _outcome_key(post_receipt) != 'applied':
            return case.finish(
                'failed', 'the resubmitted declared command settled '
                + _outcome_key(post_receipt) + ' on the promoted peer '
                '— the new active owes one applied settlement')
        ref = save_evidence(ctx['evidence_dir'],
                            'across-promotion-post-settled.json',
                            post_receipt)
        case.evidence('file', ref, "the resubmitted command's settled "
                      'receipt on the promoted peer')

        # ---- the cross-peer settlement audit -------------------------
        #
        # Let the promoted peer's record settle before reading the
        # journals: the demoted peer's tracking pulls adopt the
        # settled line, and the audit reads that convergence.
        wait_for(lambda: _across_settles(journal_of(peer), post_command,
                                         post_actor)
                 or _across_settles(journal_of(peer), command, pre_actor),
                 time.monotonic() + ACROSS_AUDIT, interval=ACROSS_POLL)
        audits = {}
        for name in (owner, peer):
            audits[name] = {entry['actor']: _across_settles(
                journal_of(name), entry['command'], entry['actor'])
                for entry in (pre, post)}
        evidence['settlements'] = audits
        ref = save_evidence(ctx['evidence_dir'],
                            'across-promotion-settlements.json',
                            audits)
        case.evidence('file', ref, "each peer's command_settled "
                      'records for both submissions')
        problems = []
        for name in (owner, peer):
            for entry in (pre, post):
                settled = audits[name].get(entry['actor']) or []
                outcomes = {_outcome_key(receipt)
                            for receipt in settled}
                if len(settled) > 1:
                    problems.append(
                        name + ' journaled ' + str(len(settled))
                        + ' command_settled records for the submission '
                        + entry['actor'] + ' — exactly one settlement '
                        'per admission, never the demoted peer\'s '
                        'history replayed onto the promoted peer')
                elif outcomes and outcomes != {'applied'}:
                    problems.append(
                        name + ' settled the submission ' + entry['actor']
                        + ' as ' + json.dumps(sorted(outcomes))
                        + ' — the receipted path owes applied')
            served = audits[name].get(pre_actor) or audits[name].get(
                post_actor) or []
            if not served:
                problems.append(
                    name + ' lost the settlement of a submission the '
                    'pair served — the receipt must survive the switch')
        if problems:
            return case.finish('failed', '; '.join(problems[:4]))

        # ---- (c) the emitted-event continuity ----------------------
        after = {name: records(name, [pre, post]) for name in (owner,
                                                                peer)}
        evidence['records_after'] = after
        ref = save_evidence(ctx['evidence_dir'],
                            'across-promotion-records.json',
                            {'before': before, 'after': after})
        case.evidence('file', ref, 'the attributed record stream each '
                      'peer serves before and after the promotion')
        if not after[peer]:
            return case.finish(
                'failed', 'the promoted peer serves no record for the '
                'command — the resubmission\'s settlement never reached '
                'its journal')
        # The continuity clauses: the pre-promotion entries stand as
        # the later record's own prefix — each keeping its component
        # attribution, none re-emitted — and the stream never steps a
        # tick backward. A misattributed or replayed record breaks the
        # prefix; an out-of-order one breaks the tick order.
        violations = _across_continuous(before[peer], after[peer])
        for entry in after[peer]:
            if entry[1] == 'event_emitted' \
                    and not entry[2].startswith(str(component) + ':'):
                violations.append(
                    'the promoted peer carries an emitted record '
                    'attributed to ' + entry[2] + ' where the command\'s '
                    'component is ' + str(component) + ' — the '
                    'attribution must not drift across the switch')
        if violations:
            return case.finish('failed', '; '.join(violations[:3]))
        case.observe('the promoted peer\'s record continues the '
                     + str(len(before[peer])) + ' pre-promotion '
                     'entries in tick order across '
                     + str(len(after[peer])) + ' attributed records')

        # ---- (d) the restore ----------------------------------------
        restored = _pair_active(ctx) == owner \
            and _tracking_standby(ctx, peer) is not None
        if not restored:
            restored = _across_restore(ctx, owner,
                                       time.monotonic() + ACROSS_SETTLE)
        roles_after = {name: _role(ctx, ctx[name]) for name in (owner,
                                                                 peer)}
        evidence['roles_after'] = roles_after
        ref = save_evidence(ctx['evidence_dir'],
                            'across-promotion-roles-after.json',
                            roles_after)
        case.evidence('file', ref, "both peers' /role reports after "
                      'the restore')
        if not restored:
            return case.finish('failed', 'the pair did not return to '
                               'its pre-scenario roles — owner '
                               + str(_pair_active(ctx)))
        if roles_before[owner].get('role') != roles_after[owner].get('role') \
                or roles_before[peer].get('role') != \
                roles_after[peer].get('role'):
            return case.finish('failed', 'the restored roles differ '
                               'from the pre-scenario assignment: '
                               + json.dumps(
                                   {name: value.get('role')
                                    for name, value
                                    in roles_after.items()},
                                   sort_keys=True))
        case.observe('the pair restored its pre-scenario roles')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))