"""The alarm_rationalization acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *


# --------------------------------------------------------------------
# Decision 70's declared-once alarm rationalization record on the
# deployed pair (WW-ALM-001's master-alarm-database clause, WW-OPS-002's
# bounded retune path). Every managed alarm instance in the rig model
# carries the record's two halves: the prose `rationalization` block —
# consequence of inaction, required action, display/procedure
# reference — served per instance on the signal index's `components`
# section, and the `priority`/`class`/`response_ticks` Int parameters
# served live on the snapshot's `parameters` section and tunable
# through the receipted `set_parameter` path. Identity lands as the
# instance plus the `Signal` on its standing `alarm` point — the
# journaled point the durable record's `point_changed` entries name.
# The case joins the served records to the registry's descriptors and
# the standing signals by the shared `kind:id` name, audits the field
# owner's --journal-file for every standing point's transition record,
# then runs one receipted `response_ticks` retune — applied, served,
# and restored to the declared value — and re-reads the sections for
# the consistency a declared-once record owes. Named diagnostics:
# rationalization-failed for a serving-contract miss,
# rationalization-nondeterministic when the same section disagrees
# with itself across reads.

RATIONALIZATION_SETTLE = 30    # bound on the pair reporting settled
RATIONALIZATION_DEADLINE = 30  # bound on each retune settle/serve wait

# The managed alarm kinds — every declared instance of either carries
# the decision-70 record.
RATIONALIZATION_KINDS = ('managed-latching-alarm',
                         'managed-bool-latching-alarm')

# The prose fields a complete rationalization block carries.
RATIONALIZATION_FIELDS = ('consequence', 'required_action',
                          'reference')

# The declared (priority, class, response_ticks) the rig model pins on
# the named instances — asserted verbatim off the served report: the
# wet-well high alarm and the per-pump fault alarms.
RATIONALIZATION_DECLARED = {
    'managed-latching-alarm:5': (1, 1, 30),
    'managed-bool-latching-alarm:22': (2, 2, 60),
    'managed-bool-latching-alarm:36': (2, 2, 60)}

# The identity joins the rig model declares on the named instances —
# instance plus the Signal on its standing alarm point.
RATIONALIZATION_STANDING = {
    'managed-latching-alarm:5': ('lah-alarm', 1003),
    'managed-bool-latching-alarm:11': ('power-fail-alarm', 1053),
    'managed-bool-latching-alarm:22': ('p101-fault-alarm', 1073)}

# The retune leg's instance — the wet-well high alarm.
RATIONALIZATION_RETUNE = 'managed-latching-alarm:5'


def _standing_alarm_point(descriptor):
    """The point a descriptor's `alarm` port binds — the standing
    output the instance-plus-Signal identity names — or None."""
    for port in descriptor.get('ports') or []:
        if isinstance(port, dict) and port.get('name') == 'alarm':
            return port.get('point')
    return None


def _journaled_point_changes(records):
    """The point set a parsed --journal-file's `point_changed` entries
    name."""
    changed = set()
    for item in records:
        observed = _journal_observation(item)
        if observed is not None and observed[0] == 'point_changed':
            changed.add(observed[1])
    return changed


def _tune_and_served(ctx, base, component, name, value, deadline):
    """One receipted `set_parameter` plus the served-report readback:
    submit through POST /command, wait for the terminal receipt at the
    captured submission index, then for the parameter report serving
    the new value. Returns the leg's audit record — `settled` is the
    receipt's normalized outcome key, None when the submission never
    reached a terminal verdict."""
    command = {'set_parameter': {'component': component, 'name': name,
                                 'value': {'int': value}}}
    record = {'command': command, 'value': value}
    index = _next_receipt_index(ctx, base)
    try:
        status, receipt = http_json('POST', base + '/command',
                                    {'command': command,
                                     'actor': 'qa-lane'})
    except urllib.error.HTTPError as exc:
        record['status'] = exc.code
        try:
            record['receipt'] = json.loads(exc.read() or b'null')
        except Exception:
            record['receipt'] = None
        finally:
            exc.close()
        return record
    record['status'] = status
    record['receipt'] = receipt
    if status != 200:
        return record
    settled = wait_for(
        lambda: _submitted_receipt(ctx, base, index, command),
        deadline)
    record['settled'] = _outcome_key(settled)
    record['served'] = bool(wait_for(
        lambda: _parameter_value(s, component, name) == value
        if (s := _try_snapshot(ctx, base)) else None,
        deadline))
    record['served_value'] = _parameter_value(
        _try_snapshot(ctx, base) or {}, component, name)
    return record


def scenario_alarm_rationalization(ctx):
    """The deployed pair serves every managed alarm instance's
    declared-once rationalization record: the prose block on the
    signal index's components section, the live declared
    priority/class/response_ticks on the snapshot's parameters, the
    standing Signal points journaled, and a receipted response_ticks
    retune applying, serving, and restoring."""
    case = Case(
        'alarm-rationalization',
        'Served alarm rationalization record and priority data',
        'with the deployed pair settled, GET /signals\' components '
        'section carries one ComponentRecord per managed alarm '
        'instance — kind:id names each carrying a complete '
        'rationalization block (consequence, required action, '
        'reference) — GET /snapshot\'s parameters section reports '
        'every instance\'s live priority/class/response_ticks with '
        'the pinned instances\' declared values, the field owner\'s '
        'durable journal carries a point_changed entry on every '
        'instance\'s standing Signal point (lah-alarm 1003, '
        'power-fail-alarm 1053, p101-fault-alarm 1073), a receipted '
        'set_parameter retune of response_ticks settles applied and '
        'serves the new value before the declared value restores, '
        'and a re-read serves the same record')
    try:
        # Self-contained on either role layout, like evidence-capture:
        # a lone replay finds the fresh rig (ctrl-a active), the full
        # suite reaches this case past the model-revision roll where
        # the revised peer owns the field — the derived document's
        # additive-only recipe leaves the alarm set untouched.
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + RATIONALIZATION_SETTLE)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        base = ctx[active]
        journal_path = (ctx.get('journal_files') or {}).get(active)
        if journal_path is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no journal-file path for the '
                               'field owner')
        case.observe('served record against ' + active + ' (' + base
                     + ')')

        _, signals = http_json('GET', base + '/signals')
        snap = _snapshot(ctx, base)
        ref = save_evidence(ctx['evidence_dir'],
                            'alarm-rationalization-signals.json',
                            signals)
        case.evidence('file', ref, 'the signal index\'s components '
                      'records and standing-point signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'alarm-rationalization-parameters.json',
                            {'descriptors': snap.get('descriptors'),
                             'parameters': snap.get('parameters')})
        case.evidence('file', ref, 'the served descriptors and the '
                      'live parameter report')

        # The coverage leg: one ComponentRecord per managed alarm
        # instance — the components section and the snapshot's
        # descriptor set must agree on the managed set — each record
        # named by the shared kind:id convention and carrying a
        # complete prose block.
        components = signals.get('components')
        parameters = snap.get('parameters')
        descriptors_wire = snap.get('descriptors')
        if not isinstance(components, list) \
                or not isinstance(parameters, list) \
                or not isinstance(descriptors_wire, list):
            return case.finish('inconclusive', 'a served section the '
                               'record joins through is absent '
                               '(components/descriptors/parameters) '
                               '— the deployed build predates the '
                               'contract')
        records = {}
        malformed = []
        for entry in components:
            if not isinstance(entry, dict) \
                    or entry.get('kind') not in RATIONALIZATION_KINDS:
                continue
            if not isinstance(entry.get('name'), str):
                malformed.append(entry)
                continue
            records[entry['name']] = entry
        descriptors = {}
        for entry in descriptors_wire:
            if isinstance(entry, dict) \
                    and entry.get('kind') in RATIONALIZATION_KINDS \
                    and isinstance(entry.get('name'), str):
                descriptors[entry['name']] = entry
        if not records and not descriptors:
            return case.finish('inconclusive', 'the served sections '
                               'omit the managed alarm records — the '
                               'deployed model is not the rig\'s '
                               'rationalized set')
        bad = []
        if malformed:
            bad.append('unnamed managed-alarm records: '
                       + json.dumps(malformed)[:200])
        if sorted(records) != sorted(descriptors):
            bad.append('the components section and the served '
                       'descriptors disagree on the managed alarm '
                       'set: '
                       + json.dumps({'records': sorted(records),
                                     'descriptors':
                                     sorted(descriptors)}))
        for name in sorted(records):
            record = records[name]
            if not name.startswith(record['kind'] + ':'):
                bad.append('record ' + name + ' is not named by its '
                           'kind:id identity')
                continue
            block = record.get('rationalization')
            missing = [field for field in RATIONALIZATION_FIELDS
                       if not isinstance((block or {}).get(field), str)
                       or not block[field].strip()]
            if not isinstance(block, dict) or missing:
                bad.append(name + ' carries no complete '
                           'rationalization block (missing '
                           + ', '.join(missing or
                                       RATIONALIZATION_FIELDS) + ')')
        if bad:
            return case.finish('failed', 'rationalization-failed: '
                               + '; '.join(bad[:4]))
        case.observe(str(len(records)) + ' managed alarm records '
                     'served with rationalization blocks')

        # The live-parameter leg: every instance's declared
        # priority/class/response_ticks reported live, the pinned
        # instances verbatim.
        reported = {}
        for entry in parameters:
            if isinstance(entry, dict) \
                    and isinstance(entry.get('name'), str):
                reported[entry['name']] = entry.get('values') or {}
        live = {}
        bad = []
        for name in sorted(records):
            if name not in reported:
                bad.append(name + ' serves no parameters entry')
                continue
            row = {}
            for param in ('priority', 'class', 'response_ticks'):
                number = _parameter_value(snap, name, param)
                if not isinstance(number, int) \
                        or isinstance(number, bool):
                    bad.append(name + ' ' + param + ' is not served '
                               'live: ' + json.dumps(number)[:80])
                else:
                    row[param] = number
            if len(row) == 3:
                live[name] = (row['priority'], row['class'],
                              row['response_ticks'])
                pin = RATIONALIZATION_DECLARED.get(name)
                if pin is not None and live[name] != pin:
                    bad.append(name + ' serves (priority, class, '
                               'response_ticks) ' + str(live[name])
                               + ' against the declared ' + str(pin))
        if bad:
            return case.finish('failed', 'rationalization-failed: '
                               + '; '.join(bad[:4]))
        case.observe('live parameters served for '
                     + str(len(live)) + ' instances')

        # The identity leg: the record's `reference` names the
        # standing Signal on the instance's bound `alarm` point —
        # instance plus signal, joined to the served records by name —
        # and the durable journal's point_changed record names that
        # point.
        named = {entry.get('name'): entry
                 for entry in signals.get('points') or []
                 if isinstance(entry, dict)}
        journal_records = _journal_entries(journal_path)
        changed = _journaled_point_changes(journal_records)
        missing_pins = [name for name in RATIONALIZATION_STANDING
                        if name not in records]
        if missing_pins:
            return case.finish('inconclusive', 'the served records '
                               'omit the pinned instances '
                               + ', '.join(sorted(missing_pins))
                               + ' — the deployed model is not the '
                               'rig\'s declared set')
        bad = []
        identity = {}
        for name in sorted(records):
            reference = records[name]['rationalization']['reference']
            signal = named.get(reference)
            if signal is None or signal.get('signal') is None:
                bad.append(name + ' reference ' + repr(reference)
                           + ' names no served Signal')
                continue
            point = signal.get('point')
            bound = _standing_alarm_point(
                descriptors.get(name) or {})
            if bound != point:
                bad.append(name + ' reference ' + repr(reference)
                           + ' lands on point ' + str(point)
                           + ' while its alarm port binds '
                           + str(bound))
                continue
            identity[name] = (reference, point)
            if point not in changed:
                bad.append('the durable journal names no '
                           'point_changed on ' + name
                           + '\'s standing point ' + str(point)
                           + ' (' + str(reference) + ')')
        for name, want in RATIONALIZATION_STANDING.items():
            got = identity.get(name)
            if got is not None and got != want:
                bad.append(name + ' joins to (signal, point) '
                           + str(got) + ' — the declared identity is '
                           + str(want))
        standing = {point for _, point in identity.values()}
        entries = []
        for item in journal_records:
            observed = _journal_observation(item)
            if observed is not None and observed[0] == 'point_changed' \
                    and observed[1] in standing:
                entries.append(item)
        ref = save_evidence(
            ctx['evidence_dir'],
            'alarm-rationalization-journal.json',
            {'path': str(journal_path),
             'standing': {name: {'signal': signal, 'point': point}
                          for name, (signal, point)
                          in identity.items()},
             'entries': entries})
        case.evidence('file', ref, 'the durable journal\'s '
                      'point_changed entries on the standing points')
        if bad:
            return case.finish('failed', 'rationalization-failed: '
                               + '; '.join(bad[:4]))
        case.observe('identity join: ' + str(len(identity))
                     + ' standing points journaled')

        # The declared-parameter half: a receipted response_ticks
        # retune applies and serves, then the declared value restores
        # the same way — the bounded-path clause.
        component = RATIONALIZATION_RETUNE
        current = live[component][2]
        tuned = current + 1
        deadline = time.monotonic() + RATIONALIZATION_DEADLINE
        tune = _tune_and_served(ctx, base, component,
                                'response_ticks', tuned, deadline)
        restore = None
        if tune.get('settled') == 'applied':
            restore = _tune_and_served(ctx, base, component,
                                       'response_ticks', current,
                                       time.monotonic()
                                       + RATIONALIZATION_DEADLINE)
        ref = save_evidence(ctx['evidence_dir'],
                            'alarm-rationalization-retune.json',
                            {'component': component, 'from': current,
                             'to': tuned, 'tune': tune,
                             'restore': restore})
        case.evidence('file', ref, 'the retune and restore '
                      'submissions, receipts, and readbacks')
        if tune.get('status') != 200 \
                or tune.get('settled') != 'applied':
            return case.finish('failed', 'rationalization-failed: '
                               'the retune never settled applied: '
                               + json.dumps(tune,
                                            sort_keys=True)[:300])
        if not tune.get('served'):
            return case.finish('failed', 'rationalization-failed: '
                               'the applied retune never served '
                               + str(tuned) + ' — the report reads '
                               + str(tune.get('served_value')))
        if not restore or restore.get('status') != 200 \
                or restore.get('settled') != 'applied':
            return case.finish('failed', 'rationalization-failed: '
                               'the restore never settled applied: '
                               + json.dumps(restore,
                                            sort_keys=True)[:300])
        if not restore.get('served'):
            return case.finish('failed', 'rationalization-failed: '
                               'the restored value never served '
                               + str(current) + ' — the report reads '
                               + str(restore.get('served_value')))
        case.observe('retune ' + component + ' response_ticks '
                     + str(current) + ' -> ' + str(tuned)
                     + ' applied and served, restored to '
                     + str(current))

        # The consistency leg: the same sections re-read after the
        # restore must serve the same record — a divergent answer is
        # the nondeterministic miss, not a contract one.
        _, signals2 = http_json('GET', base + '/signals')
        snap2 = _snapshot(ctx, base)
        records2 = {}
        for entry in signals2.get('components') or []:
            if isinstance(entry, dict) \
                    and entry.get('kind') in RATIONALIZATION_KINDS \
                    and isinstance(entry.get('name'), str):
                records2[entry['name']] = entry
        reported2 = {}
        for entry in snap2.get('parameters') or []:
            if isinstance(entry, dict) \
                    and isinstance(entry.get('name'), str):
                reported2[entry['name']] = entry.get('values') or {}
        drift = []
        for name in sorted(records):
            record2 = records2.get(name)
            if record2 is None \
                    or record2.get('rationalization') \
                    != records[name].get('rationalization'):
                drift.append(name + ' rationalization')
                continue
            row2 = tuple(_parameter_value(snap2, name, param)
                         for param in
                         ('priority', 'class', 'response_ticks'))
            if row2 != live.get(name):
                drift.append(name + ' parameters ' + str(row2)
                             + ' vs ' + str(live.get(name)))
        ref = save_evidence(
            ctx['evidence_dir'],
            'alarm-rationalization-reread.json',
            {'records': {name: (records2.get(name) or {})
                              .get('rationalization')
                         for name in sorted(records)},
             'parameters': {name: (reported2.get(name) or {})
                            for name in sorted(records)}})
        case.evidence('file', ref, 'the sections re-read after the '
                      'restore')
        if drift:
            return case.finish(
                'failed', 'rationalization-nondeterministic: the '
                'served record moved between reads: '
                + '; '.join(drift[:4]))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
