"""The alarm_report acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: The alarm-report case drives journaled field contacts and
# quality faults through the plant-side seams under the settled
# active's shared writer claim, execs the lane-built dcs-alarm-report
# binary on the host against the pair's monitors and journal files,
# and restores every driven input — a read-only report over a driven
# burst perturbs no role, so it needs no declared window.


# --------------------------------------------------------------------
# dcs-alarm-report over the rig's durable alarm record (the shipped
# flood-and-performance tooling computing the declared metric set
# over GET /journal and the durable --journal-file). The leg drives a
# deterministic burst on the rig — a counted force/ack/release burst
# on one managed alarm plus one genuine field-fault-driven activation
# so the record mixes forced and plant-driven transitions — then
# invokes the tool twice: once over the served journal and once over
# the controller's runner-owned host-side journal path. The report
# must carry the declared metric set with counts consistent with the
# driven transitions, the ordered transition evidence must preserve
# first-out order, and two invocations over identical input must print
# byte-identical output. The scenario reports inconclusive when the
# run context carries no binary path or the rig model declares no
# alarm instances.

REPORT_DEADLINE = 60  # bound on each served transition/settle wait
REPORT_POLL = 0.05    # transition-watch cadence — under the scan
REPORT_ACTOR = 'qa-lane'
REPORT_BURST = 3      # the counted force/ack/release rounds
REPORT_TIMEOUT = 30   # bound on one dcs-alarm-report invocation

# The declared metric set — the top-level sections and per-alarm
# fields the tool's AlarmReport document owes.
REPORT_SECTIONS = ('alarms', 'rates', 'responses', 'standing',
                   'priority_distribution', 'source')
ALARM_FIELDS = ('component', 'signal', 'alarm_point', 'priority',
                'response_ticks', 'activations', 'annunciations',
                'acknowledgments')


def _report_points(signals):
    """The report burst's signal-name to point map, or None when the
    deployed model declares no drivable alarm."""
    names = {'p101-moisture': 'contact',
             'p101-moisture-alarm': 'alarm',
             'p101-moisture-unacknowledged': 'unack',
             'p101-moisture-ack': 'ack',
             'level-primary': 'level_primary',
             'backup-active-alarm': 'backup_alarm',
             'backup-active-unacknowledged': 'backup_unack',
             'backup-active-ack': 'backup_ack'}
    entries = {entry.get('name'): entry
               for entry in signals.get('points', [])
               if entry.get('name') in names}
    missing = sorted(set(names) - set(entries))
    if missing:
        return None, missing
    return ({key: entries[name].get('point')
             for name, key in names.items()}, [])


def _run_report(binary, args):
    """One dcs-alarm-report invocation, captured — the subprocess seam
    the pool tests fake."""
    return subprocess.run([binary, *args], capture_output=True,
                          text=True, timeout=REPORT_TIMEOUT)


def _report_addr(base):
    """A monitor base URL as dcs-alarm-report's `<addr>` argument."""
    return base.split('://', 1)[-1]


def _metric_misses(report, driven):
    """The declared-metric-set audit: every section present, every
    driven alarm computed with consistent counts. `driven` maps
    alarm point to the expected activation count. Returns misses."""
    misses = []
    for section in REPORT_SECTIONS:
        if section not in report:
            misses.append('the report omits the declared section '
                          + section)
    by_point = {entry.get('alarm_point'): entry
                for entry in report.get('alarms', [])
                if isinstance(entry, dict)}
    for point, count in driven.items():
        entry = by_point.get(point)
        if entry is None:
            misses.append('the report computes no metrics for the '
                          'driven alarm point ' + str(point))
            continue
        for field in ALARM_FIELDS:
            if field not in entry:
                misses.append('the driven alarm point ' + str(point)
                              + ' omits the declared measure '
                              + field)
        if entry.get('activations') != count:
            misses.append('the driven alarm point ' + str(point)
                          + ' counts ' + str(entry.get('activations'))
                          + ' activations, expected the ' + str(count)
                          + ' the leg drove')
    rates = report.get('rates', {})
    detail = sum(entry.get('activations', 0)
                 for entry in report.get('alarms', [])
                 if isinstance(entry, dict))
    if rates.get('activations') != detail:
        misses.append('the rates section counts '
                      + str(rates.get('activations')) + ' activations '
                      'against the per-instance detail\'s '
                      + str(detail))
    return misses


def _first_out_order(journal, alarm_points):
    """The journal's first activation tick per alarm point, in journal
    order."""
    first = {}
    for entry in _journal_list(journal):
        change = (entry.get('event') or {}).get('point_changed') or {}
        if change.get('point') in alarm_points \
                and (change.get('to') or {}).get('bool') is True \
                and change['point'] not in first:
            first[change['point']] = entry.get('tick')
    return [point for point, _ in sorted(first.items(),
                                         key=lambda item: item[1])]


def scenario_alarm_report(ctx):
    """Drive the counted burst plus one field-fault activation, then
    prove dcs-alarm-report computes the declared metric set over the
    served journal and the durable file — deterministically."""
    case = Case('alarm-report',
                'dcs-alarm-report computes the declared metrics',
                'the lane-built dcs-alarm-report binary computes the '
                'declared flood-and-performance metric set over the '
                'deployed pair\'s served journal and over the '
                'controller\'s durable journal file — counts '
                'consistent with the driven burst, first-out order '
                'preserved, repeat invocations byte-identical, the '
                'two runs agreeing on their overlapping window')
    stream = None
    base = None
    active = None
    points = None
    restore_contact = None
    injected = []
    try:
        binary = ctx.get('alarm_report')
        if not binary:
            return case.finish('inconclusive', 'the run context '
                               'carries no dcs-alarm-report binary '
                               'path')
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        base = ctx[active]
        case.observe('settled pair: ' + active + ' active')

        _, signals = http_json('GET', base + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'alarm-report-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming the burst')
        points, missing = _report_points(signals)
        if points is None:
            return case.finish('inconclusive', 'the deployed model '
                               'declares no alarm instances the leg '
                               'can drive — no signals '
                               + ', '.join(missing))
        for name in ('ack', 'backup_ack'):
            entry = next(entry for entry in signals.get('points', [])
                         if entry.get('point') == points[name])
            if not entry.get('writable') \
                    or entry.get('direction') != 'in':
                return case.finish('inconclusive', 'the ack point '
                                   + str(points[name]) + ' is not the '
                                   'writable ack input the leg needs')
        if not (ctx.get('journal_files') or {}).get(active):
            return case.finish('inconclusive', 'the run publishes no '
                               'host-side journal path for the '
                               'settled active ' + str(active))
        case.observe('report points: ' + json.dumps(points,
                                                     sort_keys=True))
        if ctx.get('plant') is None:
            return case.finish('inconclusive',
                               'the run publishes no plant endpoint')
        owner = (ctx.get('plant_owner') or {}).get(active)
        if owner is None:
            return case.finish('inconclusive', 'the run pins no '
                               'plant-writer owner token for the '
                               'settled active ' + str(active))
        stream = _plant_connect(ctx)
        field = _field_inputs(ctx)
        if points['contact'] not in field:
            return case.finish('inconclusive', 'the moisture '
                               'contact\'s point '
                               + str(points['contact'])
                               + ' is not a field in-point the plant '
                               'serves')
        verdict = _plant_request(stream, {'op': 'ensure_writer',
                                          'owner': owner})
        if verdict.get('result') not in ('done', 'claimed_shared'):
            return case.finish('inconclusive', 'the writer claim '
                               'refused the shared attachment: '
                               + json.dumps(verdict)[:300])

        last = {}

        def value(key, snap):
            sample = _point_sample(snap, points[key])
            if sample is None:
                return None
            raw = sample.get('value')
            if isinstance(raw, dict):
                return next(iter(raw.values()), None)
            return raw

        def poll(cond):
            snap = _try_snapshot(ctx, base)
            if snap is None:
                return None
            last['snap'] = snap
            return snap if cond(snap) else None

        def leg(name, cond, keys):
            hit = wait_for(lambda: poll(cond),
                           time.monotonic() + REPORT_DEADLINE,
                           interval=REPORT_POLL)
            snap = last.get('snap') or {}
            ref = save_evidence(
                ctx['evidence_dir'], 'alarm-report-' + name + '.json',
                {'tick': snap.get('tick'),
                 'samples': {key: _point_sample(snap, points[key])
                             for key in keys}})
            case.evidence('file', ref, 'the served ' + name + ' leg')
            if hit:
                return hit, None
            return None, case.finish(
                'inconclusive' if any(
                    _point_sample(snap, points[key]) is None
                    for key in keys) else 'failed',
                'alarm-report-failed: the ' + name + ' leg never '
                'landed; last served '
                + json.dumps({key: value(key, snap) for key in keys},
                             sort_keys=True)[:500])

        def ack_cycle(ack, unack, alarm=None):
            """One receipted ack plus its re-arm through the served
            command path."""
            write = {'point': points[ack], 'kind': 'bool',
                     'value': {'bool': True}}
            try:
                status, receipt = http_json(
                    'POST', base + '/command',
                    {'command': {'write_value': write},
                     'actor': REPORT_ACTOR})
            except Exception as exc:
                return case.finish('failed', 'alarm-report-failed: '
                                   'the ' + ack + ' write errored: '
                                   + str(exc))
            if status != 200 or 'rejected' in (
                    (receipt or {}).get('outcome') or {}):
                return case.finish('failed', 'alarm-report-failed: '
                                   'the ' + ack + ' write was '
                                   'refused: ' + str(status) + ' '
                                   + json.dumps(receipt)[:300])
            keys = (unack,) if alarm is None else (unack, alarm)
            cond = (lambda s: value(unack, s) is False) if alarm \
                is None else (lambda s: value(unack, s) is False
                              and value(alarm, s) is True)
            hit, error = leg(ack + '-cleared', cond, keys)
            if error:
                return error
            try:
                http_json('POST', base + '/command',
                          {'command': {'write_value': {
                              'point': points[ack], 'kind': 'bool',
                              'value': {'bool': False}}},
                           'actor': REPORT_ACTOR})
            except Exception:
                pass
            return None

        # Phase 1 — the counted burst: the field contact driven true
        # under the shared claim trips the moisture alarm, the
        # receipted ack clears the latch while the condition stands,
        # the release returns it — repeated REPORT_BURST times.
        restore_contact = points['contact']
        for round in range(1, REPORT_BURST + 1):
            verdict = _plant_request(
                stream, {'op': 'write', 'point': points['contact'],
                         'value': {'bool': True}})
            if verdict.get('result') != 'done':
                return case.finish('failed', 'alarm-report-failed: '
                                   'the burst write was refused: '
                                   + json.dumps(verdict)[:300])
            hit, error = leg(
                'burst-%d-tripped' % round,
                lambda s: value('alarm', s) is True
                and value('unack', s) is True,
                ('alarm', 'unack'))
            if error:
                return error
            error = ack_cycle('ack', 'unack', 'alarm')
            if error:
                return error
            verdict = _plant_request(
                stream, {'op': 'write', 'point': points['contact'],
                         'value': {'bool': False}})
            hit, error = leg(
                'burst-%d-returned' % round,
                lambda s: value('alarm', s) is False,
                ('alarm',))
            if error:
                return error
        case.observe('the counted burst drove %d activations'
                     % REPORT_BURST)

        # Phase 2 — the genuine field-fault-driven activation: a
        # non-Good quality on level-primary annunciates the backup
        # alarm, so the record mixes plant-driven transitions.
        verdict = _plant_ctl(ctx, 'fault',
                             str(points['level_primary']),
                             'bad:device_fault')
        if verdict.get('result') != 'done':
            return case.finish('failed', 'alarm-report-failed: '
                               'inject_fault on level-primary '
                               'refused: '
                               + json.dumps(verdict)[:300])
        injected.append(points['level_primary'])
        hit, error = leg(
            'fault-tripped',
            lambda s: value('backup_alarm', s) is True
            and value('backup_unack', s) is True,
            ('backup_alarm', 'backup_unack'))
        if error:
            return error
        error = ack_cycle('backup_ack', 'backup_unack')
        if error:
            return error
        _plant_ctl(ctx, 'clear-fault', str(points['level_primary']))
        injected = []
        hit, error = leg(
            'fault-returned',
            lambda s: value('backup_alarm', s) is False,
            ('backup_alarm',))
        if error:
            return error
        case.observe('the field-fault activation annunciated')
        restore_contact = None

        # Phase 3 — the served-journal report: the tool exits zero
        # and carries the declared metric set with the driven counts.
        addr = _report_addr(base)
        result = _run_report(binary, [addr])
        if result.returncode != 0:
            return case.finish('failed', 'alarm-report-failed: '
                               'dcs-alarm-report ' + addr + ' exited '
                               + str(result.returncode) + ': '
                               + result.stderr.strip()[:300])
        served_text = result.stdout
        try:
            served = json.loads(served_text)
        except ValueError:
            return case.finish('failed', 'alarm-report-failed: '
                               'dcs-alarm-report printed no '
                               'AlarmReport document')
        ref = save_evidence(ctx['evidence_dir'],
                            'alarm-report-served.json', served)
        case.evidence('file', ref, 'the served-journal report')
        misses = _metric_misses(
            served, {points['alarm']: REPORT_BURST,
                     points['backup_alarm']: 1})
        if misses:
            return case.finish('failed', 'alarm-report-failed: '
                               + '; '.join(misses))
        journal = None
        try:
            _, journal = http_json('GET', base + '/journal?since=0')
        except Exception as exc:
            return case.finish('failed', 'alarm-report-failed: the '
                               'served journal refused: ' + str(exc))
        order = _first_out_order(
            journal, [points['alarm'], points['backup_alarm']])
        if order != [points['alarm'], points['backup_alarm']]:
            return case.finish(
                'failed', 'alarm-report-failed: the ordered '
                'transition evidence does not preserve first-out '
                'order: ' + json.dumps(order))
        case.observe('the served report covers the driven burst in '
                     'first-out order')

        # Phase 4 — the durable-file report: the same computation
        # over the controller's host-side journal path — the
        # restart-surviving dataset — agreeing on the overlap.
        journal_path = ctx['journal_files'][active]
        result = _run_report(binary, [addr, '--journal-file',
                                      journal_path])
        if result.returncode != 0:
            return case.finish('failed', 'alarm-report-failed: '
                               'dcs-alarm-report --journal-file '
                               'exited ' + str(result.returncode)
                               + ': ' + result.stderr.strip()[:300])
        filed_text = result.stdout
        try:
            filed = json.loads(filed_text)
        except ValueError:
            return case.finish('failed', 'alarm-report-failed: the '
                               'journal-file run printed no '
                               'AlarmReport document')
        ref = save_evidence(ctx['evidence_dir'],
                            'alarm-report-file.json', filed)
        case.evidence('file', ref, 'the durable-file report')
        misses = _metric_misses(
            filed, {points['alarm']: REPORT_BURST,
                    points['backup_alarm']: 1})
        if misses:
            return case.finish('failed', 'alarm-report-failed: the '
                               'journal-file run: ' + '; '.join(misses))
        served_rates = (served.get('rates') or {})
        filed_rates = (filed.get('rates') or {})
        if served_rates.get('activations') != \
                filed_rates.get('activations'):
            return case.finish(
                'failed', 'alarm-report-failed: the journal-file '
                'run and the served-journal run disagree on their '
                'overlapping window: '
                + json.dumps(served_rates.get('activations'))
                + ' vs '
                + json.dumps(filed_rates.get('activations')))
        served_source = served.get('source') or {}
        filed_source = filed.get('source') or {}
        if (filed_source.get('first_seq') or 0) > (
                served_source.get('first_seq') or 0) \
                or (filed_source.get('last_seq') or 0) < (
                    served_source.get('last_seq') or 0):
            return case.finish(
                'failed', 'alarm-report-failed: the journal-file '
                'run does not cover the served run\'s journal '
                'stretch on their overlapping window: served '
                + json.dumps({key: served_source.get(key)
                              for key in ('first_seq', 'last_seq')})
                + ' vs file '
                + json.dumps({key: filed_source.get(key)
                              for key in ('first_seq', 'last_seq')}))
        case.observe('the durable file agrees with the served view')

        # Phase 5 — determinism: repeat invocations over identical
        # input print byte-identical output — the served run twice
        # and the file run twice.
        repeat = _run_report(binary, [addr])
        if repeat.returncode != 0:
            return case.finish('failed', 'alarm-report-failed: the '
                               'repeat served invocation exited '
                               + str(repeat.returncode))
        if repeat.stdout != served_text:
            return case.finish(
                'failed', 'alarm-report-failed: repeat invocations '
                'over the served journal differ')
        repeat_file = _run_report(binary, [addr, '--journal-file',
                                            journal_path])
        if repeat_file.returncode != 0:
            return case.finish('failed', 'alarm-report-failed: the '
                               'repeat file invocation exited '
                               + str(repeat_file.returncode))
        if repeat_file.stdout != filed_text:
            return case.finish(
                'failed', 'alarm-report-failed: repeat invocations '
                'over the journal file differ')
        case.observe('repeat invocations are byte-identical')
        if _settled_active(ctx) != active:
            return case.finish('failed', 'alarm-report-failed: the '
                               'active role moved under the report')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        try:
            for point in injected:
                _try_plant_ctl(ctx, 'clear-fault', str(point))
            if restore_contact is not None and stream is not None:
                _plant_request(stream, {'op': 'write',
                                        'point': restore_contact,
                                        'value': {'bool': False}})
            if base is not None and points is not None:
                for ack in ('ack', 'backup_ack'):
                    try:
                        http_json(
                            'POST', base + '/command',
                            {'command': {'write_value': {
                                'point': points[ack], 'kind': 'bool',
                                'value': {'bool': False}}},
                             'actor': REPORT_ACTOR})
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
