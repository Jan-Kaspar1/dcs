"""The event_retention acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *


# --------------------------------------------------------------------
# Declared-event retention routing (WW-FND-003's EventRetention
# semantics): every served interface declares each event's retention —
# `history` routes the emission to the bounded event-history record,
# `latest` to the standing latest-emission view, and only `journal`
# emissions journal `event_emitted`. The case locates a component
# declaring both routed classes through GET /schema — model-declared,
# never hardcoded — drives the declared emission path through the
# receipted command surface the same registry exposes, and asserts on
# the active's /resources that each emission adds an event-history
# record for the History name while the Latest name only refreshes
# its standing record: repeated emissions grow the ring and overwrite
# the view. On the tracking standby the descriptor is served but the
# command path refuses at the role boundary — the tracking peer emits
# the adopted run's events identically (decision 84), so the parity
# this leg pins is that the routed events never journal there: no
# event_emitted records accrue for the driven run's History/Latest
# names, and the command-driven path stays refused until promotion.

RETENTION_DEADLINE = 30    # bound on the tracking and served-record waits
RETENTION_MAX_DRIVES = 4   # receipted candidates tried per run
RETENTION_LEAK_POLLS = 3   # journal reads the standby accrual watch takes


def _routed_declarations(interface):
    """The interface's History/Latest-declared event names:
    {name: 'history'|'latest'} — the retention classes the serving
    layer routes to their own stores instead of journaling."""
    declared = {}
    for spec in (interface or {}).get('events') or []:
        if spec.get('retention') in ('history', 'latest') \
                and spec.get('name'):
            declared[str(spec['name'])] = spec['retention']
    return declared


def _emission_records(view, component):
    """The event_emitted records one /resources component entry
    carries, keyed by emitted event name — the attributed journal tail
    beside the routed History/Latest records, each entry's `retention`
    naming the store it landed in."""
    found = {}
    for entry in (view or {}).get('components') or []:
        if entry.get('name') != component:
            continue
        for record in entry.get('events') or []:
            emitted = ((record or {}).get('event') or {}) \
                .get('event_emitted', {}).get('event') or {}
            if emitted.get('component') != component \
                    or not emitted.get('event'):
                continue
            found.setdefault(str(emitted['event']), []).append(record)
    return found


def _journal_emitted(payload, component, names):
    """The event_emitted records a journal payload carries for `names`
    on `component` — the durable-side accrual a routed emission must
    never produce."""
    hits = []
    for entry in _journal_list(payload):
        emitted = (entry.get('event') or {}).get('event_emitted', {}) \
            .get('event') or {}
        if emitted.get('component') == component \
                and emitted.get('event') in names:
            hits.append(entry)
    return hits


def _record_key(record):
    """One served event record's identity — a superseded Latest record
    reads as a fresh key while an unchanged one does not."""
    emitted = ((record.get('event') or {}).get('event_emitted', {})
               .get('event') or {})
    return (emitted.get('event'), record.get('retention'),
            record.get('seq'), record.get('tick'),
            json.dumps(emitted.get('fields'), sort_keys=True))


def _emission_drives(component, interface, signals):
    """The receipted-path submissions that can drive the component's
    declared emissions, in preference order: the kind-declared
    commands first, then the adapted set_parameter/write_value
    surface, then writes against the writable bound point an
    on_observed_change event names."""
    candidates = []
    for spec in (interface or {}).get('commands') or []:
        submission = _command_for_spec(component, spec)
        if submission is None:
            continue
        rank = 0 if spec.get('adapted') == 'declared' else 1
        candidates.append((rank, json.dumps(submission,
                                            sort_keys=True),
                           submission))
    candidates.sort(key=lambda item: (item[0], item[1]))
    drives = [submission for _, _, submission in candidates]
    points = {entry.get('point'): entry
              for entry in signals.get('points', [])}
    defaults = {'bool': {'bool': True}, 'int': {'int': 1},
                'float': {'float': 1.0}}
    seen = {json.dumps(drive, sort_keys=True) for drive in drives}
    for spec in (interface or {}).get('events') or []:
        if spec.get('emission') != 'on_observed_change':
            continue
        entry = points.get(spec.get('point'))
        if entry is None or not entry.get('writable') \
                or entry.get('direction') != 'in':
            continue
        value = defaults.get(entry.get('value_type'))
        if value is None:
            continue
        drive = {'write_value': {'point': spec['point'],
                                 'kind': entry['value_type'],
                                 'value': value}}
        if json.dumps(drive, sort_keys=True) not in seen:
            drives.append(drive)
    return drives


def _repeat_drive(submission):
    """The same drive again — a value-bearing variant flips its
    argument so the repeat still changes state where a held write to
    the same value would observe no transition."""
    repeat = json.loads(json.dumps(submission))
    for variant in ('write_value', 'set_parameter'):
        body = repeat.get(variant)
        if not isinstance(body, dict):
            continue
        value = body.get('value')
        if isinstance(value, dict):
            if 'bool' in value:
                value['bool'] = not value['bool']
            elif 'int' in value:
                value['int'] = value['int'] + 1
            elif 'float' in value:
                value['float'] = value['float'] + 1.0
    return repeat


def scenario_event_retention(ctx):
    """Declared event retentions route to their own served stores —
    History accumulates, Latest overwrites, and the tracking standby
    journals none of the routed emissions."""
    case = Case('event-retention',
                'Declared event retention routes to its stores',
                'a served component declaring both History and Latest '
                'event retentions is located through GET /schema and '
                'driven through the receipted command path the '
                'interface exposes: on the active each emission adds '
                'an event-history record for the History name while '
                'the Latest name only refreshes its standing record, '
                'and on the tracking standby the descriptor is '
                'published, the command path refuses, and no '
                'event_emitted journal records accrue for the routed '
                'events')
    try:
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        base = ctx[active]
        case.observe('retention routing against ' + active
                     + ' (' + base + ')')

        _, signals = http_json('GET', base + '/signals')
        _, schema = http_json('GET', base + '/schema')
        _, resources = http_json('GET', base + '/resources')
        _, history = http_json('GET', base + '/history?since=0')
        _, journal = http_json('GET', base + '/journal?since=0')
        ref = save_evidence(ctx['evidence_dir'],
                            'event-retention-schema.json', schema)
        case.evidence('file', ref, 'the served registry the qualifying '
                      'component is located through')
        ref = save_evidence(ctx['evidence_dir'],
                            'event-retention-served.json',
                            {'signals': signals, 'resources': resources,
                             'history': history, 'journal': journal})
        case.evidence('file', ref, 'the signal index, resource, '
                      'point-history, and journal records before the '
                      'driven emissions')

        # The qualifying component is model-declared, never hardcoded:
        # an instance the registry declares — and the resource view
        # serves — carrying at least one History-retained and one
        # Latest-retained event.
        served = {str(entry.get('name'))
                  for entry in resources.get('components') or []
                  if entry.get('name')}
        qualifying = None
        for entry in schema.get('interfaces') or []:
            interface = (entry or {}).get('interface') or {}
            declared = _routed_declarations(interface)
            if 'history' in declared.values() \
                    and 'latest' in declared.values() \
                    and str(entry.get('name')) in served:
                qualifying = (str(entry.get('name')), interface,
                              declared)
                break
        if qualifying is None:
            return case.finish('inconclusive', 'the served registry '
                               'declares no component carrying both '
                               'History and Latest event retentions')
        component, interface, declared = qualifying
        case.observe('qualifying component: ' + component
                     + ' declares '
                     + json.dumps(declared, sort_keys=True))
        drives = _emission_drives(component, interface, signals)
        if not drives:
            return case.finish('inconclusive', component + ' exposes '
                               'no receipted command or bound-point '
                               'path the declared emissions can be '
                               'driven through')

        # `seen` holds every served record key so a poll's diff is the
        # new emissions; `harvest` merges each fresh /resources view.
        seen = set()
        view = {'last': resources}

        def harvest(payload):
            new = []
            for name, recs in _emission_records(payload,
                                                component).items():
                for record in recs:
                    key = _record_key(record)
                    if key in seen:
                        continue
                    seen.add(key)
                    new.append((name, record))
            return new

        def collect():
            try:
                _, latest = http_json('GET', base + '/resources')
            except Exception:
                return None
            view['last'] = latest
            return harvest(latest) or None

        def misrouted(records):
            return [name + ' declared ' + declared[name]
                    + ' retained in the '
                    + str(record.get('retention')) + ' store'
                    for name, record in records
                    if declared.get(name)
                    and record.get('retention') != declared[name]]

        def accumulated(payload):
            """Latest-declared events standing as more than one
            record in a single view — the overwrite that wasn't."""
            counts = {}
            for name, recs in _emission_records(payload,
                                                component).items():
                if declared.get(name) != 'latest':
                    continue
                counts[name] = sum(
                    1 for record in recs
                    if record.get('retention') == 'latest')
            return [name + ' accumulates standing latest records'
                    for name, count in counts.items() if count > 1]

        failures = misrouted(harvest(resources)) + accumulated(resources)
        produced = {}
        covered = {}
        drives_log = []
        for index, submission in enumerate(
                drives[:RETENTION_MAX_DRIVES]):
            if failures or len(covered) == 2:
                break
            status, receipt = http_json(
                'POST', base + '/command',
                {'command': submission, 'actor': 'qa-lane'})
            entry = {'submission': submission, 'status': status,
                     'receipt': receipt}
            drives_log.append(entry)
            if status != 200 or not isinstance(receipt, dict) \
                    or not isinstance(receipt.get('outcome'), dict):
                ref = save_evidence(ctx['evidence_dir'],
                                    'event-retention-drives.json',
                                    drives_log)
                case.evidence('file', ref, 'the driven submissions '
                              'and their receipts')
                return case.finish('failed', 'the emission path '
                                   'returned no structured receipt: '
                                   + str(status) + ' '
                                   + json.dumps(receipt)[:300])
            entry['outcome'] = _outcome_key(receipt)
            if 'rejected' in (receipt.get('outcome') or {}):
                case.observe('drive refused: ' + entry['outcome'])
                continue
            new = wait_for(collect,
                           time.monotonic() + RETENTION_DEADLINE,
                           interval=POLL_INTERVAL) or []
            produced[index] = sorted({name for name, _ in new})
            entry['produced'] = produced[index]
            case.observe('drive '
                         + json.dumps(submission, sort_keys=True)[:160]
                         + ' produced '
                         + (', '.join(produced[index])
                            if produced[index] else 'no emissions'))
            failures += misrouted(new) + accumulated(view['last'])
            for name in produced[index]:
                retention = declared.get(name)
                if retention and retention not in covered:
                    covered[retention] = index

        # The repeat leg: each drive that produced a routed class runs
        # once more — the History side must grow with the fresh
        # emission and the Latest side must overwrite, never
        # accumulate and never sit stale.
        repeats = {}
        if not failures:
            for index in sorted(set(covered.values())):
                submission = _repeat_drive(drives[index])
                status, receipt = http_json(
                    'POST', base + '/command',
                    {'command': submission, 'actor': 'qa-lane'})
                entry = {'submission': submission, 'status': status,
                         'receipt': receipt, 'repeat': True}
                drives_log.append(entry)
                entry['outcome'] = _outcome_key(receipt) \
                    if isinstance(receipt, dict) else 'none'
                if status != 200 or 'rejected' in (
                        (receipt or {}).get('outcome') or {}):
                    repeats[index] = None
                    continue
                new = wait_for(collect,
                               time.monotonic() + RETENTION_DEADLINE,
                               interval=POLL_INTERVAL) or []
                entry['produced'] = sorted({name for name, _ in new})
                failures += misrouted(new) + accumulated(view['last'])
                repeats[index] = new
        ref = save_evidence(ctx['evidence_dir'],
                            'event-retention-drives.json', drives_log)
        case.evidence('file', ref, 'the driven submissions, their '
                      'receipts, and the events each produced')
        ref = save_evidence(ctx['evidence_dir'],
                            'event-retention-resources.json',
                            view['last'])
        case.evidence('file', ref, 'the served resource records the '
                      'routed emissions landed in')
        if failures:
            return case.finish('failed', '; '.join(failures))
        missing = [cls for cls in ('history', 'latest')
                   if cls not in covered]
        if missing:
            return case.finish(
                'inconclusive', 'the reachable emission paths produced '
                'no ' + '-retained or '.join(missing)
                + '-retained emission for ' + component)
        unproven = [index for index, new in repeats.items() if not new]
        if unproven:
            return case.finish(
                'inconclusive', 'the repeated drive produced no '
                'observable emissions — the retention growth check '
                'cannot run')
        unverified = []
        for index, new in repeats.items():
            fresh = {name for name, _ in new}
            rerun = [name for name in (produced.get(index) or [])
                     if name in declared]
            if not any(name in fresh for name in rerun):
                unverified.append(index)
                continue
            for name in rerun:
                if name in fresh:
                    continue
                if declared[name] == 'history':
                    failures.append(
                        'the repeated emission did not grow the '
                        'event-history record for ' + name)
                else:
                    failures.append(
                        'the repeated emission left the '
                        'latest-emission record for ' + name
                        + ' stale')
        if failures:
            return case.finish('failed', '; '.join(failures))
        if unverified:
            return case.finish(
                'inconclusive', 'the repeated drive emitted none of '
                'the first run\'s routed events — the retention '
                'growth check cannot run')
        case.observe('repeated emissions grew the history record and '
                     'refreshed the standing latest records')

        # The routed events never journal — on either peer.
        _, journal = http_json('GET', base + '/journal?since=0')
        ref = save_evidence(ctx['evidence_dir'],
                            'event-retention-journal.json', journal)
        case.evidence('file', ref, 'the active\'s journal after the '
                      'driven emissions')
        hits = _journal_emitted(journal, component, set(declared))
        if hits:
            return case.finish(
                'failed', 'the active journaled event_emitted records '
                'for routed events: '
                + json.dumps(hits, sort_keys=True)[:400])

        # The tracking-standby leg: the descriptor publishes, the
        # command-driven emission path refuses at the role boundary —
        # emission through it resumes only after promotion — and no
        # event_emitted journal records accrue for the routed names.
        peer = wait_for(lambda: _tracking_peer(ctx, active),
                        time.monotonic() + RETENTION_DEADLINE,
                        interval=POLL_INTERVAL)
        if peer is None:
            return case.finish('inconclusive', 'no pair peer reports '
                               'a tracking standby — the suppression '
                               'leg cannot be exercised')
        peer_base = ctx[peer]
        _, peer_schema = http_json('GET', peer_base + '/schema')
        _, peer_resources = http_json('GET', peer_base + '/resources')
        peer_interface = next(
            ((entry or {}).get('interface') or {}
             for entry in peer_schema.get('interfaces') or []
             if entry.get('name') == component), {})
        descriptor = set(declared) \
            <= set(_routed_declarations(peer_interface))
        published = any(entry.get('name') == component
                        for entry in
                        peer_resources.get('components') or [])
        submission = drives[sorted(set(covered.values()))[0]]
        try:
            status, refusal = http_json(
                'POST', peer_base + '/command',
                {'command': submission, 'actor': 'qa-lane'})
            refused = status != 200 or 'rejected' in (
                (refusal or {}).get('outcome') or {})
        except urllib.error.HTTPError as exc:
            status, refusal, refused = exc.code, str(exc.code), True
        leak = {'journal': None}
        hits = []
        for _poll in range(RETENTION_LEAK_POLLS):
            try:
                _, leak['journal'] = http_json(
                    'GET', peer_base + '/journal?since=0')
            except Exception:
                continue
            hits += _journal_emitted(leak['journal'], component,
                                     set(declared))
            if hits:
                break
            time.sleep(POLL_INTERVAL)
        ref = save_evidence(ctx['evidence_dir'],
                            'event-retention-standby.json',
                            {'peer': peer, 'schema': peer_schema,
                             'resources': peer_resources,
                             'submission': submission,
                             'status': status, 'receipt': refusal,
                             'journal': leak['journal']})
        case.evidence('file', ref, 'the tracking standby\'s '
                      'descriptor, refusal, and journal')
        if not (descriptor and published):
            return case.finish('failed', 'the tracking standby '
                               'publishes no descriptor for '
                               + component)
        if not refused:
            return case.finish('failed', 'the tracking standby '
                               'admitted the driven command instead '
                               'of refusing it at the role boundary')
        if hits:
            return case.finish('failed', 'the tracking standby '
                               'journaled event_emitted records for '
                               'the driven run')
        case.observe('the tracking standby publishes the descriptor, '
                     'refuses the drive, and journals none of the '
                     'routed events')
        return case.finish('passed')
    except urllib.error.HTTPError as exc:
        return case.finish('failed', 'a served endpoint answered '
                           + str(exc.code))
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
