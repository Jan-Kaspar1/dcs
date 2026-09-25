"""The served_interface acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *


# --------------------------------------------------------------------
# The served block-interface contract (WW-FND-003, decision 82): every
# assessed run proves the schema-driven surface the tranche ships —
# GET /schema's registry covering every kind the rig model declares
# with all five collections, a declared command settling through the
# receipted command path, and GET /resources' emitted-events view
# reflecting a produced event.

INTERFACE_COLLECTIONS = ('measurements', 'configuration', 'state',
                         'commands', 'events')
CONTRACT_DEADLINE = 30  # bound on the receipt and emitted-event waits


def scenario_served_interface(ctx):
    """The served block-interface contract against the rig: registry
    coverage, a receipted declared command, and the emitted-events
    view reflecting the produced event."""
    case = Case('served-interface',
                'Served block-interface contract covers the model',
                'GET /schema covers every component kind the rig model '
                'declares with all five collections, a declared command '
                'submitted through POST /command returns a structured '
                'receipt, and GET /resources attributes a produced '
                'event to the issuing instance')
    try:
        # Self-contained on either role layout, like evidence-capture:
        # replayed alone the rig is fresh (ctrl-a active), while the
        # full suite reaches this case after the failover.
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        base = ctx[active]
        case.observe('served contract against ' + active
                     + ' (' + base + ')')

        # Each documented endpoint is part of the served contract: an
        # answered error means the surface itself is missing — a
        # failed check, where a monitor that cannot be reached at all
        # stays inconclusive.
        bodies = {}
        for path in ('/signals', '/schema', '/resources'):
            try:
                _, bodies[path] = http_json('GET', base + path)
            except urllib.error.HTTPError as exc:
                return case.finish('failed', 'GET ' + path
                                   + ' answered ' + str(exc.code))
        signals, schema = bodies['/signals'], bodies['/schema']
        ref = save_evidence(ctx['evidence_dir'],
                            'served-interface-signals.json', signals)
        case.evidence('file', ref, 'declared component records')
        ref = save_evidence(ctx['evidence_dir'],
                            'served-interface-schema.json', schema)
        case.evidence('file', ref, 'the served interface registry')

        declared = signals.get('components') or []
        if not declared:
            return case.finish('inconclusive',
                               'the signal index serves no component '
                               'records to check coverage against')
        served = {}
        for entry in schema.get('interfaces') or []:
            if isinstance(entry, dict):
                served[entry.get('name')] = entry.get('interface') or {}
        missing = [record for record in declared
                   if (served.get(record.get('name')) or {}).get('kind')
                   != record.get('kind')]
        if missing:
            return case.finish(
                'failed', 'the served registry misses declared kinds '
                + ', '.join(sorted({str(r.get('kind'))
                                    for r in missing}))
                + ' (instances: '
                + ', '.join(str(r.get('name')) for r in missing[:8])
                + ')')
        short = {}
        for entry in schema.get('interfaces') or []:
            interface = (entry or {}).get('interface') or {}
            absent = [name for name in INTERFACE_COLLECTIONS
                      if not isinstance(interface.get(name), list)]
            if absent:
                short[str(entry.get('name'))] = absent
        if short:
            return case.finish(
                'failed', 'served interfaces miss collections: '
                + json.dumps(short, sort_keys=True)[:600])
        kinds = sorted({str(record.get('kind')) for record in declared})
        case.observe('registry covers ' + str(len(declared))
                     + ' declared instances across '
                     + str(len(kinds)) + ' kinds ('
                     + ', '.join(kinds) + ') at publication '
                     + str(schema.get('publication'))
                     + ' tick ' + str(schema.get('tick')))

        picked = _pick_declared_command(
            schema.get('interfaces') or [], signals)
        if picked is None:
            return case.finish('inconclusive',
                               'no served command translates to the '
                               'receipted path')
        component, spec, command = picked
        case.observe('declared command: ' + str(component) + ' '
                     + str(spec.get('name')) + ' -> '
                     + json.dumps(command, sort_keys=True))
        try:
            status, receipt = http_json(
                'POST', base + '/command',
                {'command': command, 'actor': 'qa-lane'})
        except urllib.error.HTTPError as exc:
            return case.finish('failed', 'the declared command '
                               'returned no receipt: HTTP '
                               + str(exc.code))
        ref = save_evidence(ctx['evidence_dir'],
                            'served-interface-receipt.json',
                            {'status': status, 'body': receipt})
        case.evidence('file', ref, 'the declared command\'s receipt')
        outcome = receipt.get('outcome') \
            if isinstance(receipt, dict) else None
        if status != 200 or not isinstance(receipt, dict) \
                or not isinstance(receipt.get('command'), dict) \
                or not isinstance(outcome, dict) or not outcome:
            return case.finish(
                'failed', 'the declared command returned no '
                'structured receipt: ' + str(status) + ' '
                + json.dumps(receipt)[:400])
        case.observe('receipt outcome: '
                     + json.dumps(outcome, sort_keys=True))

        # The emitted-events view is the instance's attributed journal
        # tail: the produced event is the submission's settled receipt
        # — journaled whether it applied or refused — or a kind-
        # emitted event the run produced.
        observed = {'events': None, 'match': None}

        def events_cover():
            try:
                _, view = http_json('GET', base + '/resources')
            except urllib.error.HTTPError:
                raise
            except Exception:
                return None
            for entry in view.get('components') or []:
                if entry.get('name') != component:
                    continue
                observed['events'] = entry.get('events') or []
                for candidate in observed['events']:
                    event = (candidate or {}).get('event') or {}
                    settled = (event.get('command_settled') or {}) \
                        .get('receipt') or {}
                    if settled.get('command') == command \
                            or event.get('event_emitted'):
                        observed['match'] = candidate
                        return True
            return None

        covered = wait_for(events_cover,
                           time.monotonic() + CONTRACT_DEADLINE,
                           interval=POLL_INTERVAL)
        ref = save_evidence(
            ctx['evidence_dir'], 'served-interface-events.json',
            {'component': component, 'match': observed['match'],
             'events': observed['events'] or []})
        case.evidence('file', ref, 'emitted events attributed to '
                      + str(component))
        if not covered:
            return case.finish(
                'failed', 'the emitted-events view never reflected a '
                'produced event for ' + str(component))
        match = (observed['match'] or {}).get('event') or {}
        case.observe('emitted-events view covers '
                     + next(iter(match), '?') + ' for '
                     + str(component) + ' ('
                     + str(len(observed['events'] or []))
                     + ' entries)')
        return case.finish('passed')
    except urllib.error.HTTPError as exc:
        return case.finish('failed', 'the emitted-events view answered '
                           + str(exc.code))
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
