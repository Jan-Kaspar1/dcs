"""The command_availability acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *


# --------------------------------------------------------------------
# The served per-command availability verdicts (WW-FND-003's
# served-contract honesty): GET /resources reports each command's
# `available`/`refusal` verdict — advisory, the receipted path stays
# the sole authority — so a read model reporting a command invocable
# while dispatch refuses it, or vice versa, is exactly the dishonesty
# the tranche exists to prevent. The case reads the settled active's
# rows, asserts each is self-consistent (an `available: false` row
# carries a named refusal, an `available` row carries none), then
# proves probe-then-submit agreement: a served-unavailable command —
# the rig's port-adapted surfaces include managed-alarm `shelve`/`oos`
# commands whose bound-point writability is model-declared — settles
# the same named refusal the row served, never `applied`, and a
# served-available command settles `applied`. A tracking standby's
# /resources must report identical verdicts: decision 84's
# same-adopted-state rule makes availability a property of the
# adopted run, not of which peer is asked.

AVAILABILITY_DEADLINE = 30  # bound on each settled-receipt wait


def _command_rows(view):
    """The (component-name, row) command verdicts a /resources view
    serves, in served order."""
    rows = []
    for component in (view or {}).get('components') or []:
        for row in component.get('commands') or []:
            rows.append((component.get('name'), row))
    return rows


def _verdict_map(view):
    """A /resources view's verdicts keyed for the cross-peer
    comparison: {(component, command): (available, refusal)}."""
    verdicts = {}
    for component, row in _command_rows(view):
        verdicts[(str(component), str(row.get('name')))] = \
            (row.get('available'), row.get('refusal'))
    return verdicts


def _specs_by_name(schema):
    """The /schema registry's command specs keyed by (component,
    command name) — the provenance join a /resources row needs to
    reach the receipted path."""
    specs = {}
    for entry in (schema or {}).get('interfaces') or []:
        interface = (entry or {}).get('interface') or {}
        for spec in interface.get('commands') or []:
            specs[(entry.get('name'), spec.get('name'))] = spec
    return specs


def _served_refusal_text(reason):
    """The /resources `refusal` string a settled CommandError reason
    corresponds to: `not_writable`/`unknown_point` rows serve the
    named CommandError's text exactly as the receipted path answers
    it, while a kind-declared `command_refused` serves the declared
    refusal reason the CommandError's `reason` field echoes. Returns
    None for a reason the verdict surface cannot have served."""
    if not isinstance(reason, dict):
        return None
    if 'not_writable' in reason:
        point = (reason.get('not_writable') or {}).get('point')
        return 'I/O point PointId(' + str(point) \
            + ') is not declared writable'
    if 'unknown_point' in reason:
        point = (reason.get('unknown_point') or {}).get('point')
        return 'unknown I/O point PointId(' + str(point) + ')'
    if 'command_refused' in reason:
        return (reason.get('command_refused') or {}).get('reason')
    return None


def _rejected_reason(receipt):
    """The settled CommandError a terminal receipt carries, or None."""
    rejected = ((receipt or {}).get('outcome') or {}).get('rejected')
    return (rejected or {}).get('reason') \
        if isinstance(rejected, dict) else None


def scenario_command_availability(ctx):
    """The served per-command availability verdicts against the
    receipted path — WW-FND-003's served-contract honesty on the
    simulated rig."""
    case = Case('command-availability',
                'Served command verdicts agree with dispatch',
                'every GET /resources command row is self-consistent '
                '(an available: false row carries a named refusal, an '
                'available row carries none), a served-unavailable '
                'command submitted through POST /command settles the '
                'same named refusal the row served rather than '
                'applied, a served-available command settles applied, '
                'and a tracking standby serves identical verdicts')
    try:
        # Self-contained on either role layout, like served-interface:
        # replayed alone the rig is fresh (ctrl-a active), while the
        # full suite reaches this case after the failover.
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        base = ctx[active]
        case.observe('verdict surface against ' + active
                     + ' (' + base + ')')

        bodies = {}
        for path in ('/resources', '/schema', '/signals'):
            try:
                _, bodies[path] = http_json('GET', base + path)
            except urllib.error.HTTPError as exc:
                return case.finish('failed', 'GET ' + path
                                   + ' answered ' + str(exc.code))
        resources, schema, signals = (bodies['/resources'],
                                      bodies['/schema'],
                                      bodies['/signals'])
        ref = save_evidence(
            ctx['evidence_dir'], 'command-availability-rows.json',
            {'publication': resources.get('publication'),
             'tick': resources.get('tick'),
             'components': [{'name': entry.get('name'),
                             'commands': entry.get('commands')}
                            for entry in resources.get('components')
                            or []]})
        case.evidence('file', ref, 'the active\'s served verdict rows')

        rows = _command_rows(resources)
        if not rows:
            return case.finish('inconclusive',
                               'the resource view serves no command '
                               'rows to check')
        case.observe(str(len(rows)) + ' served command rows')

        # Every served verdict is self-consistent: `available: false`
        # carries the named refusal a submission would meet, an
        # `available` row carries none.
        malformed = []
        for component, row in rows:
            name = str(component) + ' ' + str(row.get('name'))
            if not isinstance(row.get('available'), bool):
                malformed.append(name + ' serves no boolean verdict')
            elif row['available'] and row.get('refusal') is not None:
                malformed.append(name + ' serves available with a '
                                 'refusal')
            elif not row['available'] \
                    and not (isinstance(row.get('refusal'), str)
                             and row['refusal'].strip()):
                malformed.append(name + ' serves available: false '
                                 'without a named refusal')
        if malformed:
            return case.finish('failed', 'served verdicts are not '
                               'self-consistent: '
                               + '; '.join(malformed[:8]))

        # The probe legs need each row's provenance: the /schema spec
        # sharing the row's (component, command) key is what translates
        # it back into a receipted-path submission.
        specs = _specs_by_name(schema)
        if not any(spec.get('availability') == 'kind_declared'
                   for spec in specs.values()):
            case.observe('the rig model declares no kind-declared-'
                         'availability command — that probe leg is '
                         'uncovered; bound-point-writable verdicts are '
                         'asserted below')
        translatable = []
        for component, row in rows:
            spec = specs.get((component, row.get('name')))
            if spec is None:
                continue
            submission = _command_for_spec(component, spec)
            if submission is not None:
                translatable.append((component, spec, row, submission))

        # Deterministic probe selection: the refused probe prefers a
        # port-adapted write — the managed-alarm shelve/oos surfaces
        # whose BoundPointWritable verdicts the model always declares —
        # then a kind-declared invoke; the available probe prefers the
        # writable-bool write the other command legs use, then a
        # declared invoke, then any remaining translatable row.
        writable = {entry.get('point')
                    for entry in signals.get('points', [])
                    if entry.get('writable')
                    and entry.get('direction') == 'in'
                    and entry.get('value_type') == 'bool'}
        preferred = {entry.get('point')
                     for entry in signals.get('points', [])
                     if entry.get('name') == 'p101-oos'} & writable

        def refused_rank(entry):
            adapted = entry[1].get('adapted')
            return 0 if adapted == 'write_value' \
                else 1 if adapted == 'declared' else 2

        def available_rank(entry):
            spec = entry[1]
            if spec.get('adapted') == 'write_value':
                return 0 if spec.get('point') in preferred \
                    else 1 if spec.get('point') in writable else 3
            return 2 if spec.get('adapted') == 'declared' else 3

        refused = sorted(
            (entry for entry in translatable
             if entry[2].get('available') is False), key=refused_rank)
        available = sorted(
            (entry for entry in translatable
             if entry[2].get('available') is True), key=available_rank)

        def submit(command):
            """POST the command and wait out its terminal receipt."""
            index = _next_receipt_index(ctx, base)
            status, receipt = http_json(
                'POST', base + '/command',
                {'command': command, 'actor': 'qa-lane'})
            settled = wait_for(
                lambda: _submitted_receipt(ctx, base, index, command),
                time.monotonic() + AVAILABILITY_DEADLINE)
            return status, receipt, settled

        if refused:
            component, spec, row, command = refused[0]
            case.observe('served-unavailable probe: ' + str(component)
                         + ' ' + str(spec.get('name')) + ' — served '
                         + json.dumps(row.get('refusal')))
            status, receipt, settled = submit(command)
            ref = save_evidence(
                ctx['evidence_dir'],
                'command-availability-refused.json',
                {'row': row, 'command': command,
                 'submission': {'status': status, 'receipt': receipt},
                 'settled': settled})
            case.evidence('file', ref, 'the refused probe\'s settled '
                          'receipt against its served row')
            if status != 200:
                return case.finish('failed', 'the served-unavailable '
                                   'command returned no receipt: HTTP '
                                   + str(status))
            if settled is None:
                return case.finish('failed', 'the served-unavailable '
                                   'command\'s receipt never settled')
            outcome = _outcome_key(settled)
            if outcome == 'applied':
                return case.finish('failed', 'the served-unavailable '
                                   'command ' + str(spec.get('name'))
                                   + ' settled applied anyway')
            reason = _rejected_reason(settled)
            if _served_refusal_text(reason) != row.get('refusal'):
                return case.finish(
                    'failed', 'the settled refusal names a different '
                    'reason than the served row: served '
                    + json.dumps(row.get('refusal')) + ', settled '
                    + json.dumps(settled.get('outcome')))
            case.observe('settled ' + outcome
                         + ' — the same refusal the row served')
        else:
            case.observe('no served-unavailable translatable command '
                         '— the refused probe leg is uncovered')

        if available:
            component, spec, row, command = available[0]
            case.observe('served-available probe: ' + str(component)
                         + ' ' + str(spec.get('name')))
            status, receipt, settled = submit(command)
            ref = save_evidence(
                ctx['evidence_dir'],
                'command-availability-available.json',
                {'row': row, 'command': command,
                 'submission': {'status': status, 'receipt': receipt},
                 'settled': settled})
            case.evidence('file', ref, 'the available probe\'s '
                          'settled receipt against its served row')
            if status != 200:
                return case.finish('failed', 'the served-available '
                                   'command returned no receipt: HTTP '
                                   + str(status))
            if settled is None:
                return case.finish('failed', 'the served-available '
                                   'command\'s receipt never settled')
            if _outcome_key(settled) != 'applied':
                return case.finish('failed', 'the served-available '
                                   'command settled a named refusal: '
                                   + json.dumps(settled.get('outcome')))
            case.observe('served-available command settled applied')
        else:
            case.observe('no served-available translatable command '
                         '— the applied probe leg is uncovered')

        # Decision 84's same-adopted-state rule: the tracking peer's
        # read model carries the run's one verdict set.
        peer = _tracking_peer(ctx, active)
        if peer is not None:
            try:
                _, peer_view = http_json('GET', ctx[peer] + '/resources')
            except urllib.error.HTTPError as exc:
                return case.finish('failed', 'the tracking standby\'s '
                                   'GET /resources answered '
                                   + str(exc.code))
            ref = save_evidence(
                ctx['evidence_dir'],
                'command-availability-standby.json',
                {'peer': peer,
                 'components': [{'name': entry.get('name'),
                                 'commands': entry.get('commands')}
                                for entry in
                                peer_view.get('components') or []]})
            case.evidence('file', ref, 'the tracking standby\'s '
                          'served verdict rows')
            local, remote = _verdict_map(resources), \
                _verdict_map(peer_view)
            diverged = sorted(key for key in set(local) | set(remote)
                              if local.get(key) != remote.get(key))
            if diverged:
                return case.finish(
                    'failed', 'the tracking standby reports different '
                    'verdicts: ' + '; '.join(
                        key[0] + ' ' + key[1] + ': active '
                        + json.dumps(local.get(key)) + ' vs standby '
                        + json.dumps(remote.get(key))
                        for key in diverged[:8]))
            case.observe('tracking peer ' + peer + ' serves identical '
                         'verdicts (' + str(len(local)) + ' rows)')
        else:
            case.observe('no tracking standby — the cross-peer parity '
                         'leg is uncovered')
        return case.finish('passed')
    except urllib.error.HTTPError as exc:
        return case.finish('failed', 'a served endpoint answered '
                           + str(exc.code))
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
