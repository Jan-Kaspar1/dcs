"""The source_failover acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *


# --------------------------------------------------------------------
# The source-failover leg (WW-OPS-003's declared-failover clause and
# WW-CTL-002's alarmed backup-source transition): the complementary
# primary-faulted half the backup-health header leaves to this issue.
# Everything the leg drives is model-declared wiring discovered
# through the served registry — the failover-select instance names its
# bound primary/backup source points through GET /schema's
# measurements and GET /signals resolves their direction and names,
# the flag's managed alarm is the served instance whose `in` binds the
# flag's declared <name>-in carrier, and the threshold-chain
# instance's bound level/demand plus its served setpoints back the
# demand-evaluation check — never a hardcoded id. The injected
# bad:device_fault applies at the field read seam through the shipped
# dcs-plant-ctl exactly as the field-fault leg drives it: the
# selection must move to the backup — `out` tracking the backup's
# live value at Good, never the degraded source nor a frozen
# last-known — backup_active asserts, the wired alarm stands
# unacknowledged on its declared-journaled points, and the chain keeps
# evaluating the failover-fed level rather than stalling demand. The
# clear re-selects the primary, releases backup_active, returns the
# standing alarm, and leaves the unacknowledged latch standing until
# the receipted ack settles applied — the two-flag lifecycle unchanged
# by the excursion. No role change follows: a field fault is not peer
# loss.

SOURCE_FAILOVER_DEADLINE = 30  # bound on each surfacing/settlement wait
SOURCE_FAILOVER_ACTOR = 'qa-lane'
SOURCE_FAILOVER_OUT_SLACK = 4  # scans a fresh selected output may lag


def _interface_resources(entry):
    """The served interface entry's named resources across the
    measurements and state collections — {name: resource} — the
    bound-point wiring the schema serves per port."""
    resources = {}
    interface = entry.get('interface') or {}
    for collection in ('measurements', 'state'):
        for resource in interface.get(collection) or []:
            if isinstance(resource, dict) and resource.get('name'):
                resources[resource['name']] = resource
    return resources


def _advance_demand(held, level, table):
    """The threshold-chain's documented one-scan demand rule — the
    value the served demand must equal for the chain to be evaluating
    the failover-fed level rather than holding a stalled or fallback
    answer."""
    if level <= table['stop']:
        return 0
    if held == 2 and level <= table['start']:
        return 1
    if level >= table['lag_start']:
        return 2
    if held == 0 and level >= table['start']:
        return 1
    return held


def scenario_source_failover(ctx):
    """An injected Bad primary selects the healthy backup — served,
    alarmed, journaled, demand-live — and the clear re-selects the
    primary while the latch stands for the receipted ack."""
    case = Case('source-failover',
                'Primary-source failover selects the backup and '
                're-selects on recovery',
                'on the settled active, an injected bad:device_fault '
                'on the failover-select\'s primary field source — the '
                'instance and its bound primary/backup sources located '
                'through GET /schema and GET /signals — moves the '
                'selection to the backup: the selected level tracks '
                'the backup point\'s live value at Good quality rather '
                'than clamping or holding last-known, backup_active '
                'asserts, the wired managed alarm stands '
                'unacknowledged on its declared-journaled points, and '
                'the threshold chain keeps evaluating on the '
                'failover-fed level; clearing the fault re-selects '
                'the primary, releases backup_active, returns the '
                'standing alarm, and leaves the unacknowledged latch '
                'standing until the receipted ack settles applied — '
                'the two-flag lifecycle unchanged — and no role '
                'change follows')
    injected = None       # the primary field point, while faulted
    restore_ack = None    # (base, point) while the ack write stands
    clear_latch = None    # (base, point) while a standing latch is owed
    try:
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        base = ctx[active]
        if ctx.get('plant') is None:
            return case.finish('inconclusive',
                               'the run publishes no plant endpoint')
        case.observe('observing ' + active + ' (' + base
                     + '); plant tool at ' + str(ctx['plant']))

        _, schema = http_json('GET', base + '/schema')
        _, signals = http_json('GET', base + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'source-failover-schema.json',
                            {'schema': schema, 'signals': signals})
        case.evidence('file', ref, 'the served interface registry '
                      'and SignalIndex the wiring resolves through')

        select = None
        for entry in schema.get('interfaces') or []:
            if (entry.get('interface') or {}).get('kind') \
                    != 'failover-select':
                continue
            resources = _interface_resources(entry)
            if all((resources.get(name) or {}).get('point') is not None
                   for name in ('primary', 'backup', 'out',
                                'backup_active')):
                select = (entry, resources)
                break
        if select is None:
            return case.finish(
                'inconclusive', 'the rig model declares no '
                'failover-select instance wired with primary, backup, '
                'out, and backup_active')
        select_entry, resources = select
        points = {name: resources[name].get('point')
                  for name in ('primary', 'backup', 'out',
                               'backup_active')}
        if (resources.get('backup_unhealthy') or {}).get('point') \
                is not None:
            points['backup_unhealthy'] = \
                resources['backup_unhealthy'].get('point')
        select_name = select_entry.get('name')

        sigmap = {entry.get('point'): entry
                  for entry in signals.get('points', [])}
        for key in ('primary', 'backup'):
            entry = sigmap.get(points[key])
            if entry is None or entry.get('direction') != 'in' \
                    or entry.get('value_type') != 'float':
                return case.finish(
                    'inconclusive', 'the failover-select\'s ' + key
                    + ' source point ' + str(points[key])
                    + ' is not a served float field input: '
                    + json.dumps(entry)[:300])
        if points['primary'] == points['backup']:
            return case.finish('inconclusive', 'the failover-select '
                               'binds one source point twice — no '
                               'distinct pair to switch between')

        # The alarmed transition's managed alarm: the served instance
        # whose `in` binds the flag's declared <name>-in carrier — the
        # model's carrier convention, verified live below when the
        # carrier follows backup_active into the standing alarm.
        flag_name = (sigmap.get(points['backup_active']) or {}) \
            .get('name')
        carrier = None
        if flag_name:
            for entry in signals.get('points', []):
                if entry.get('name') == str(flag_name) + '-in' \
                        and entry.get('direction') == 'in':
                    carrier = entry.get('point')
                    break
        alarm_res = None
        if carrier is not None:
            for entry in schema.get('interfaces') or []:
                res = _interface_resources(entry)
                if (res.get('in') or {}).get('point') != carrier:
                    continue
                if all((res.get(name) or {}).get('point') is not None
                       for name in ('alarm', 'unacknowledged', 'ack')):
                    alarm_res = res
                    break
        if alarm_res is None:
            return case.finish('inconclusive', 'no managed alarm is '
                               'wired to the failover-select\'s '
                               'backup_active flag ' + str(flag_name))
        points['carrier'] = carrier
        for name in ('alarm', 'unacknowledged', 'ack'):
            points[name] = alarm_res[name].get('point')
        ack_entry = sigmap.get(points['ack']) or {}
        if not ack_entry.get('writable') \
                or ack_entry.get('direction') != 'in' \
                or ack_entry.get('value_type') != 'bool':
            return case.finish('inconclusive', 'the alarm\'s ack '
                               'point is not the declared writable '
                               'bool input: '
                               + json.dumps(ack_entry)[:300])

        # The failover-fed demand path: the threshold-chain instance's
        # bound level/demand points and whichever standing flags it
        # wires.
        chain = None
        for entry in schema.get('interfaces') or []:
            if (entry.get('interface') or {}).get('kind') \
                    != 'threshold-chain':
                continue
            res = _interface_resources(entry)
            if (res.get('level') or {}).get('point') is not None \
                    and (res.get('demand') or {}).get('point') \
                    is not None:
                chain = (entry, res)
                break
        if chain is None:
            return case.finish('inconclusive', 'the served registry '
                               'declares no threshold-chain evaluating '
                               'the failover-fed level')
        chain_entry, chain_res = chain
        chain_name = chain_entry.get('name')
        points['level'] = chain_res['level'].get('point')
        points['demand'] = chain_res['demand'].get('point')
        flags = {name: chain_res[name].get('point')
                 for name in ('duty_call', 'lag_call', 'below_cutoff',
                              'high_level')
                 if (chain_res.get(name) or {}).get('point') is not None}
        case.observe('failover wiring through ' + str(select_name)
                     + ': ' + json.dumps({k: points[k]
                                          for k in sorted(points)},
                                         sort_keys=True))

        # Two distinct healthy field source points, per the field's
        # own census — the pair the switch may choose between.
        field = _field_inputs(ctx)
        for key in ('primary', 'backup'):
            if points[key] not in field:
                return case.finish('inconclusive', 'the ' + key
                                   + ' source point '
                                   + str(points[key]) + ' is not a '
                                   'field in-point the plant serves')
        healthy_field = all(
            _quality_key((field[points[key]].get('sample') or {})
                         .get('quality')) == 'good'
            for key in ('primary', 'backup'))
        ref = save_evidence(ctx['evidence_dir'],
                            'source-failover-field.json',
                            {'points': {k: points[k]
                                        for k in sorted(points)},
                             'field': {str(points[k]): field[points[k]]
                                       for k in ('primary', 'backup')}})
        case.evidence('file', ref, 'the field census over the bound '
                      'source points')
        if not healthy_field:
            return case.finish('inconclusive', 'no two distinct '
                               'healthy field source points to '
                               'switch between')

        last = {}

        def settled():
            snap = _try_snapshot(ctx, base)
            if snap is None:
                return None
            last['snap'] = snap
            for key in ('primary', 'backup'):
                if _quality_key((_point_sample(snap, points[key])
                                 or {}).get('quality')) != 'good':
                    return None
            if _point_value(snap, points['out']) \
                    != _point_value(snap, points['primary']):
                return None
            if _point_value(snap, points['backup_active']) is not False \
                    or _point_value(snap, points['alarm']) is not False \
                    or _point_value(snap, points['unacknowledged']) \
                    is not False:
                return None
            if 'backup_unhealthy' in points \
                    and _point_value(snap, points['backup_unhealthy']) \
                    is not False:
                return None
            if _point_sample(snap, points['level']) is None \
                    or _point_sample(snap, points['demand']) is None:
                return None
            return snap

        baseline = wait_for(settled,
                            time.monotonic() + SOURCE_FAILOVER_DEADLINE,
                            interval=POLL_INTERVAL)
        snap = last.get('snap') or {}
        ref = save_evidence(ctx['evidence_dir'],
                            'source-failover-baseline.json',
                            {'tick': snap.get('tick'),
                             'samples': {key: _point_sample(
                                             snap, points[key])
                                         for key in sorted(points)}})
        case.evidence('file', ref, 'the settled healthy baseline the '
                      'excursion departs from')
        if not baseline:
            return case.finish('inconclusive', 'the failover path '
                               'never read settled-healthy ahead of '
                               'the injection')

        table = {key: _parameter_value(baseline, chain_name, key)
                 for key in ('cutoff', 'stop', 'start', 'lag_start',
                             'high', 'on_bad_demand')}
        if any(not isinstance(table[key], (int, float))
               or isinstance(table[key], bool)
               or not math.isfinite(table[key])
               for key in ('cutoff', 'stop', 'start', 'lag_start',
                           'high')) \
                or not isinstance(table['on_bad_demand'], int) \
                or isinstance(table['on_bad_demand'], bool):
            return case.finish('inconclusive', 'the threshold-chain '
                               'serves no usable setpoint table: '
                               + json.dumps(table)[:300])
        case.observe('the ' + str(chain_name) + ' setpoints: '
                     + json.dumps(table, sort_keys=True))

        # The journal floor ahead of the excursion: this leg's records
        # are the ones above it.
        _, journal0 = http_json('GET', base + '/journal?since=0')
        floor = max((entry.get('seq') or 0
                     for entry in _journal_list(journal0)), default=0)

        verdict = _plant_ctl(ctx, 'fault', str(points['primary']),
                             'bad:device_fault')
        ref = save_evidence(ctx['evidence_dir'],
                            'source-failover-inject.json',
                            {'point': points['primary'],
                             'fault': 'bad:device_fault',
                             'verdict': verdict})
        case.evidence('file', ref, 'the inject_fault verdict')
        if verdict.get('result') != 'done':
            return case.finish('failed', 'inject_fault on the primary '
                               'source refused: '
                               + json.dumps(verdict)[:300])
        injected = points['primary']
        case.observe('injected bad:device_fault on primary field '
                     'point ' + str(injected))

        def failed_over():
            snap = _try_snapshot(ctx, base)
            if snap is None:
                return None
            last['snap'] = snap
            if _quality_key((_point_sample(snap, points['primary'])
                             or {}).get('quality')) \
                    != 'bad:device_fault':
                return None
            backup = _point_sample(snap, points['backup'])
            if _quality_key((backup or {}).get('quality')) != 'good':
                return None
            selected = _point_sample(snap, points['out'])
            tick = (selected or {}).get('tick')
            if _quality_key((selected or {}).get('quality')) != 'good' \
                    or (selected or {}).get('value') \
                    != (backup or {}).get('value') \
                    or not isinstance(tick, int) \
                    or (snap.get('tick') or 0) - tick \
                    > SOURCE_FAILOVER_OUT_SLACK:
                return None
            if _point_value(snap, points['backup_active']) is not True \
                    or _point_value(snap, points['carrier']) is not True:
                return None
            if _point_value(snap, points['alarm']) is not True \
                    or _point_value(snap, points['unacknowledged']) \
                    is not True:
                return None
            if 'backup_unhealthy' in points \
                    and _point_value(snap, points['backup_unhealthy']) \
                    is not False:
                return None
            return snap

        hit = wait_for(failed_over, time.monotonic()
                       + SOURCE_FAILOVER_DEADLINE,
                       interval=POLL_INTERVAL)
        snap = last.get('snap') or {}
        ref = save_evidence(ctx['evidence_dir'],
                            'source-failover-failed-over.json',
                            {'tick': snap.get('tick'),
                             'samples': {key: _point_sample(
                                             snap, points[key])
                                         for key in sorted(points)}})
        case.evidence('file', ref, 'the served failover state — '
                      'backup selected at Good, alarm latched')
        if not hit:
            unmet = []
            if _quality_key((_point_sample(snap, points['primary'])
                             or {}).get('quality')) \
                    != 'bad:device_fault':
                unmet.append('the primary never served the injected '
                             'bad:device_fault')
            backup = _point_sample(snap, points['backup'])
            if _quality_key((backup or {}).get('quality')) != 'good':
                unmet.append('the backup degraded alongside the '
                             'injected primary')
            selected = _point_sample(snap, points['out'])
            if _quality_key((selected or {}).get('quality')) != 'good' \
                    or (selected or {}).get('value') \
                    != (backup or {}).get('value'):
                unmet.append('the selection kept tracking the '
                             'degraded source or a frozen last-known')
            if _point_value(snap, points['backup_active']) is not True:
                unmet.append('backup_active never asserted')
            if _point_value(snap, points['alarm']) is not True:
                unmet.append('the wired alarm never stood')
            if _point_value(snap, points['unacknowledged']) is not True:
                unmet.append('the alarm never latched unacknowledged')
            return case.finish('failed', 'the primary-source fault '
                               'never failed over: '
                               + '; '.join(unmet))
        case.observe('backup serving at '
                     + json.dumps(_point_value(hit, points['out']))
                     + ' Good; backup_active, alarm, and '
                     'unacknowledged standing')
        clear_latch = (base, points['ack'])
        if _settled_active(ctx) != active:
            return case.finish('failed', 'a primary-source field '
                               'fault moved the active role to '
                               + str(_settled_active(ctx)))

        # The failover-fed demand path keeps evaluating while the
        # backup serves: the chain's level input carries the selected
        # output's image at Good and the demand sample advances its
        # per-scan evaluation — never frozen, never the untrusted-input
        # fallback while the level reads healthy.
        demand = {'prev': None, 'first_tick': None, 'latest_tick': None,
                  'violations': {}, 'recent': []}

        def demand_continues():
            snap = _try_snapshot(ctx, base)
            if snap is None:
                return None
            last['demand_snap'] = snap
            level = _point_sample(snap, points['level'])
            demand_sample = _point_sample(snap, points['demand'])
            if level is None or demand_sample is None:
                return None
            lv = _point_value(snap, points['level'])
            dv = _point_value(snap, points['demand'])
            if _quality_key(level.get('quality')) != 'good':
                demand['violations']['level-quality'] = \
                    'the failover-fed level served ' \
                    + _quality_key(level.get('quality'))
            demand['recent'].append(
                _point_value(snap, points['out']))
            del demand['recent'][:-3]
            if len(demand['recent']) > 1 and lv not in demand['recent']:
                demand['violations']['level-source'] = \
                    'the chain\'s level input is not the failover ' \
                    'output\'s image: ' + json.dumps(lv)
            if isinstance(dv, int) and not isinstance(dv, bool) \
                    and isinstance(lv, (int, float)) \
                    and not isinstance(lv, bool):
                if demand['prev'] is not None:
                    want = _advance_demand(demand['prev'], lv, table)
                    if dv != want:
                        demand['violations']['diverged'] = \
                            'demand ' + json.dumps(dv) + ' is not ' \
                            'the failover-fed evaluation ' \
                            + json.dumps(want) + ' of level ' \
                            + json.dumps(lv)
                demand['prev'] = dv
                expected = {
                    'duty_call': dv >= 1,
                    'lag_call': dv >= 2,
                    'below_cutoff': lv <= table['cutoff'],
                    'high_level': lv >= table['high']}
                for name, want in expected.items():
                    if name in flags and _point_value(
                            snap, flags[name]) is not want:
                        demand['violations'][name] = \
                            name + ' served ' + json.dumps(
                                _point_value(snap, flags[name])) \
                            + ' for demand ' + json.dumps(dv) \
                            + ' on level ' + json.dumps(lv)
            tick = demand_sample.get('tick')
            if demand['first_tick'] is None:
                demand['first_tick'] = tick
            demand['latest_tick'] = tick
            return isinstance(tick, int) \
                and isinstance(demand['first_tick'], int) \
                and tick > demand['first_tick'] and snap

        wait_for(demand_continues,
                 time.monotonic() + SOURCE_FAILOVER_DEADLINE,
                 interval=POLL_INTERVAL)
        snap = last.get('demand_snap') or {}
        ref = save_evidence(ctx['evidence_dir'],
                            'source-failover-demand.json',
                            {'tick': snap.get('tick'),
                             'level': _point_sample(
                                 snap, points['level']),
                             'demand': _point_sample(
                                 snap, points['demand']),
                             'first_tick': demand['first_tick'],
                             'latest_tick': demand['latest_tick'],
                             'violations': sorted(
                                 demand['violations'])})
        case.evidence('file', ref, 'the failover-fed demand '
                      'evaluation while the backup served')
        if demand['first_tick'] is None:
            return case.finish('inconclusive', 'the failover-fed '
                               'demand path never reported on the '
                               'served snapshot')
        if demand['violations']:
            return case.finish('failed', 'demand stalled while the '
                               'backup served healthy: '
                               + '; '.join(demand['violations'][key]
                                           for key in sorted(
                                               demand['violations'])))
        if not isinstance(demand['latest_tick'], int) \
                or demand['latest_tick'] <= demand['first_tick']:
            return case.finish('failed', 'the demand evaluation froze '
                               'while the backup served healthy — '
                               'tick held ' + json.dumps(
                                   demand['latest_tick']))
        case.observe('demand kept evaluating the failover-fed level '
                     'through tick ' + str(demand['latest_tick']))

        found = {}
        violations = {}

        def journaled():
            try:
                _, journal = http_json(
                    'GET', base + '/journal?since=' + str(floor))
            except Exception:
                return None
            last['journal'] = journal
            changes = _journal_point_changes(journal)
            for key in ('backup_active', 'alarm', 'unacknowledged'):
                if {'bool': True} in changes.get(points[key], []):
                    found[key] = True
            if changes.get(points['carrier']):
                violations['unjournaled-carrier'] = \
                    'the alarm\'s unjournaled carrier point ' \
                    + str(points['carrier']) + ' journaled ' \
                    + json.dumps(changes[points['carrier']])
            if any('role_changed' in (entry.get('event') or {})
                   for entry in _journal_list(journal)):
                violations['role-change'] = \
                    'a primary-source field fault journaled a ' \
                    'role_changed'
            return (len(found) == 3 or violations) and journal

        wait_for(journaled, time.monotonic() + SOURCE_FAILOVER_DEADLINE,
                 interval=POLL_INTERVAL)
        journal = last.get('journal') or {}
        ref = save_evidence(
            ctx['evidence_dir'], 'source-failover-journal.json',
            {'floor': floor, 'found': sorted(found),
             'violations': sorted(violations),
             'entries': _journal_list(journal)})
        case.evidence('file', ref, 'the journaled failover and '
                      'alarmed transitions above the floor')
        if violations:
            return case.finish('failed', '; '.join(
                violations[key] for key in sorted(violations)))
        if len(found) != 3:
            return case.finish('failed', 'the failover\'s '
                               'transitions never journaled (found '
                               + json.dumps(sorted(found)) + ')')
        case.observe('the failover, standing alarm, and '
                     'unacknowledged latch each journaled on their '
                     'declared-journaled points; the carrier left '
                     'unjournaled')

        verdict = _plant_ctl(ctx, 'clear-fault', str(points['primary']))
        ref = save_evidence(ctx['evidence_dir'],
                            'source-failover-clear.json',
                            {'point': points['primary'],
                             'verdict': verdict})
        case.evidence('file', ref, 'the clear_fault verdict')
        if verdict.get('result') != 'done':
            return case.finish('failed', 'clear_fault on the primary '
                               'source refused: '
                               + json.dumps(verdict)[:300])
        injected = None
        case.observe('fault cleared on primary field point '
                     + str(points['primary']))

        def recovered():
            snap = _try_snapshot(ctx, base)
            if snap is None:
                return None
            last['snap'] = snap
            primary = _point_sample(snap, points['primary'])
            if _quality_key((primary or {}).get('quality')) != 'good':
                return None
            selected = _point_sample(snap, points['out'])
            if _quality_key((selected or {}).get('quality')) != 'good' \
                    or (selected or {}).get('value') \
                    != (primary or {}).get('value'):
                return None
            if _point_value(snap, points['backup_active']) is not False:
                return None
            if _point_value(snap, points['alarm']) is not False:
                return None
            if _point_value(snap, points['unacknowledged']) is not True:
                return None
            return snap

        hit = wait_for(recovered, time.monotonic()
                       + SOURCE_FAILOVER_DEADLINE,
                       interval=POLL_INTERVAL)
        snap = last.get('snap') or {}
        ref = save_evidence(ctx['evidence_dir'],
                            'source-failover-recovered.json',
                            {'tick': snap.get('tick'),
                             'samples': {key: _point_sample(
                                             snap, points[key])
                                         for key in sorted(points)}})
        case.evidence('file', ref, 'the post-clear recovery — primary '
                      're-selected, latch still standing')
        if not hit:
            unmet = []
            if _quality_key((_point_sample(snap, points['primary'])
                             or {}).get('quality')) != 'good':
                unmet.append('the primary never recovered Good')
            selected = _point_sample(snap, points['out'])
            if _quality_key((selected or {}).get('quality')) != 'good' \
                    or (selected or {}).get('value') \
                    != (_point_sample(snap, points['primary']) or {}) \
                    .get('value'):
                unmet.append('the selection never re-selected the '
                             'primary')
            if _point_value(snap, points['backup_active']) is not False:
                unmet.append('backup_active never released')
            if _point_value(snap, points['alarm']) is not False:
                unmet.append('the standing alarm never returned')
            if _point_value(snap, points['unacknowledged']) is not True:
                unmet.append('the unacknowledged latch released '
                             'before the declared ack — the two-flag '
                             'lifecycle changed by the excursion')
            return case.finish('failed', 'the failover never '
                               'recovered: ' + '; '.join(unmet))
        case.observe('primary re-selected at '
                     + json.dumps(_point_value(hit, points['out']))
                     + ' Good; backup_active released, alarm returned, '
                     'unacknowledged still latched')

        found = {}
        violations = {}

        def journaled_return():
            try:
                _, journal = http_json(
                    'GET', base + '/journal?since=' + str(floor))
            except Exception:
                return None
            last['journal'] = journal
            changes = _journal_point_changes(journal)
            for key in ('backup_active', 'alarm'):
                if {'bool': False} in changes.get(points[key], []):
                    found[key] = True
            if {'bool': False} in changes.get(points['unacknowledged'],
                                              []):
                violations['unacknowledged-release'] = \
                    'the unacknowledged latch journaled a release ' \
                    'the declared ack never wrote'
            if any('role_changed' in (entry.get('event') or {})
                   for entry in _journal_list(journal)):
                violations['role-change'] = \
                    'a primary-source field fault journaled a ' \
                    'role_changed'
            return (len(found) == 2 or violations) and journal

        wait_for(journaled_return,
                 time.monotonic() + SOURCE_FAILOVER_DEADLINE,
                 interval=POLL_INTERVAL)
        journal = last.get('journal') or {}
        ref = save_evidence(
            ctx['evidence_dir'],
            'source-failover-return-journal.json',
            {'floor': floor, 'found': sorted(found),
             'violations': sorted(violations),
             'entries': _journal_list(journal)})
        case.evidence('file', ref, 'the journaled return transitions '
                      'above the floor')
        if violations:
            return case.finish('failed', '; '.join(
                violations[key] for key in sorted(violations)))
        if len(found) != 2:
            return case.finish('failed', 'the recovery\'s return '
                               'transitions never journaled (found '
                               + json.dumps(sorted(found)) + ')')

        # The acknowledgment leg: a receipted write on the alarm's
        # declared ack input releases the standing latch — the
        # two-flag lifecycle's declared path — and the settlement
        # journals applied and attributed.
        write = {'point': points['ack'], 'kind': 'bool',
                 'value': {'bool': True}}
        status, receipt = http_json(
            'POST', base + '/command',
            {'command': {'write_value': write},
             'actor': SOURCE_FAILOVER_ACTOR})
        ref = save_evidence(ctx['evidence_dir'],
                            'source-failover-ack-receipt.json',
                            {'status': status, 'body': receipt})
        case.evidence('file', ref, 'the acknowledged ack '
                      'submission receipt')
        outcome = (receipt or {}).get('outcome') or {}
        if status != 200 or 'rejected' in outcome:
            return case.finish('failed', 'the alarm\'s declared ack '
                               'write was refused: ' + str(status)
                               + ' ' + json.dumps(receipt)[:400])
        restore_ack = (base, points['ack'])

        def acknowledged():
            snap = _try_snapshot(ctx, base)
            if snap is None:
                return None
            last['snap'] = snap
            if _point_value(snap, points['unacknowledged']) is not False:
                return None
            return snap

        hit = wait_for(acknowledged, time.monotonic()
                       + SOURCE_FAILOVER_DEADLINE,
                       interval=POLL_INTERVAL)
        snap = last.get('snap') or {}
        ref = save_evidence(ctx['evidence_dir'],
                            'source-failover-acknowledged.json',
                            {'tick': snap.get('tick'),
                             'alarm': _point_sample(snap,
                                                    points['alarm']),
                             'unacknowledged': _point_sample(
                                 snap, points['unacknowledged'])})
        case.evidence('file', ref, 'the post-ack latch state')
        if not hit:
            return case.finish('failed', 'the declared ack write '
                               'never cleared the standing '
                               'unacknowledged latch')
        case.observe('the receipted ack released the standing latch')

        settled = {}

        def journaled_ack():
            try:
                _, journal = http_json(
                    'GET', base + '/journal?since=' + str(floor))
            except Exception:
                return None
            last['journal'] = journal
            for receipt_ in _settled_receipts(journal):
                if (receipt_.get('command') or {}).get('write_value') \
                        == write:
                    settled['receipt'] = receipt_
            if {'bool': False} in _journal_point_changes(journal).get(
                    points['unacknowledged'], []):
                settled['unack_cleared'] = True
            if 'receipt' in settled and 'unack_cleared' in settled:
                return journal
            return None

        wait_for(journaled_ack,
                 time.monotonic() + SOURCE_FAILOVER_DEADLINE,
                 interval=POLL_INTERVAL)
        journal = last.get('journal') or {}
        ref = save_evidence(
            ctx['evidence_dir'], 'source-failover-ack-journal.json',
            {'floor': floor,
             'receipt': settled.get('receipt'),
             'unack_cleared': settled.get('unack_cleared'),
             'entries': _journal_list(journal)})
        case.evidence('file', ref, 'the journaled ack settlement and '
                      'latch release above the floor')
        settled_receipt = settled.get('receipt')
        if settled_receipt is None:
            return case.finish('failed', 'the ack\'s CommandSettled '
                               'never journaled')
        if 'applied' not in (settled_receipt.get('outcome') or {}):
            return case.finish('failed', 'the ack receipt did not '
                               'settle applied: '
                               + json.dumps(settled_receipt
                                            .get('outcome'))[:200])
        if settled_receipt.get('actor') != SOURCE_FAILOVER_ACTOR:
            return case.finish('failed', 'the journaled ack receipt '
                               'is unattributed: actor='
                               + json.dumps(settled_receipt
                                            .get('actor')))
        if not settled.get('unack_cleared'):
            return case.finish('failed', 'the unacknowledged flag\'s '
                               'release never journaled')
        case.observe('ack settled applied, journaled attributed to '
                     + SOURCE_FAILOVER_ACTOR + ', unacknowledged '
                     'released through the declared path')

        # Restore the rig for later cases: the operator ack point back
        # to its declared initial through the same receipted path —
        # a standing true would hold the latch clear for every later
        # leg — and the field fault is already cleared.
        restore = {'point': points['ack'], 'kind': 'bool',
                   'value': {'bool': False}}
        status, receipt = http_json(
            'POST', base + '/command',
            {'command': {'write_value': restore},
             'actor': SOURCE_FAILOVER_ACTOR})
        ref = save_evidence(ctx['evidence_dir'],
                            'source-failover-restored.json',
                            {'status': status, 'body': receipt})
        case.evidence('file', ref, 'the ack restore receipt')
        outcome = (receipt or {}).get('outcome') or {}
        if status != 200 or 'rejected' in outcome:
            return case.finish('failed', 'the ack restore write was '
                               'refused: ' + str(status) + ' '
                               + json.dumps(receipt)[:400])

        def restored():
            snap = _try_snapshot(ctx, base)
            if snap is None:
                return None
            last['snap'] = snap
            return _point_value(snap, points['ack']) is False and snap

        if not wait_for(restored,
                        time.monotonic() + SOURCE_FAILOVER_DEADLINE,
                        interval=POLL_INTERVAL):
            return case.finish('failed', 'the ack restore write '
                               'never landed on the served snapshot')
        restore_ack = None
        clear_latch = None
        if _settled_active(ctx) != active:
            return case.finish('failed', 'the excursion moved the '
                               'active role to '
                               + str(_settled_active(ctx)))
        return case.finish('passed', 'the injected primary fault '
                           'selected the healthy backup — tracked at '
                           'Good on the selected output, annunciated '
                           'through the wired alarm\'s two-flag '
                           'lifecycle, demand live — and the clear '
                           're-selected the primary while the latch '
                           'stood for the receipted ack')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        # The injected point is the run's shared field, the ack point
        # the alarm's operator input, and the standing latch the
        # lifecycle's record: a case that leaves any of them standing
        # poisons every later scenario, so the latch is cleared through
        # the same receipted ack path when the leg could not finish it.
        if injected is not None:
            try:
                _plant_ctl(ctx, 'clear-fault', str(injected))
            except Exception:
                pass
        if restore_ack is not None:
            rbase, rpoint = restore_ack
            try:
                http_json('POST', rbase + '/command',
                          {'command': {'write_value': {
                              'point': rpoint, 'kind': 'bool',
                              'value': {'bool': False}}},
                           'actor': SOURCE_FAILOVER_ACTOR})
            except Exception:
                pass
        elif clear_latch is not None:
            rbase, rpoint = clear_latch
            for value in (True, False):
                try:
                    http_json('POST', rbase + '/command',
                              {'command': {'write_value': {
                                  'point': rpoint, 'kind': 'bool',
                                  'value': {'bool': value}}},
                               'actor': SOURCE_FAILOVER_ACTOR})
                except Exception:
                    pass
