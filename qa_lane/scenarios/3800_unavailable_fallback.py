"""The unavailable_fallback acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *


# --------------------------------------------------------------------
# The all-level-sources-bad declared fallback (WW-OPS-003's
# bad-measurement leg and WW-CTL-002's failover-first contract —
# decision 42, the pumping-station note's recorded answer to the
# all-measurement-bad assumption): with every level source untrusted
# the failover-select has no good source, `out` serves the failed
# backup verbatim — Bad — and the threshold chain takes its declared
# `on_bad_demand` rather than controlling on the failed measurement.
# The legs drive the same shipped `dcs-plant-ctl` fault/clear-fault
# seam the field-fault and backup-health cases use, against the
# station's served wiring: a backup-only fault asserts the
# `backup_unhealthy` annunciation while the healthy primary keeps the
# field and no failover transition journals; faulting the primary too
# drives the declared fallback with the alarmed engagement journaled;
# and clearing in the reverse order resumes primary control and drops
# the annunciation through the named journaled transitions. The
# recovery half is where the requirement's "no unintended output step"
# clause lands: every served snapshot is checked against the declared
# selection and fallback rules, so a step to stale, substituted, or
# untrusted-data-driven output fails the leg it was observed in.

FALLBACK_DEADLINE = 30  # bound on one injection or clear surfacing


def _literal_bool(value):
    """A served `{'bool': v}` value literal — or a bare bool — unwrapped."""
    if isinstance(value, dict):
        return value.get('bool')
    return value


def _float_of(sample):
    """A served sample's unwrapped numeric value, or None."""
    value = (sample or {}).get('value')
    if isinstance(value, dict):
        value = next(iter(value.values()), None)
    return value


def _good_finite(sample):
    """Whether a served sample reads Good with a finite numeric value —
    the selector's own trustworthy-measurement predicate."""
    value = _float_of(sample)
    return _quality_key((sample or {}).get('quality')) == 'good' \
        and isinstance(value, (int, float)) and math.isfinite(value)


def _journal_transitions(body, point):
    """The (from, to) value transitions a /journal payload records on
    `point` — the point_changed entries of a declared-journaled
    carrier."""
    seen = []
    for entry in _journal_list(body):
        change = (entry.get('event') or {}).get('point_changed')
        if isinstance(change, dict) and change.get('point') == point:
            seen.append((change.get('from'), change.get('to')))
    return seen


def _fallback_violation(snap, points, on_bad):
    """The declared-rule violation one served snapshot shows, or None:
    the failover-select's `out` carries the selected source's sample —
    the primary while it reads Good and finite, else the backup
    verbatim, quality included — and the threshold chain emits its
    declared `on_bad_demand` whenever its delivered level input is
    untrusted. A snapshot missing a needed sample is one lost
    observation, never a verdict."""
    primary = _point_sample(snap, points['primary'])
    backup = _point_sample(snap, points['backup'])
    out = _point_sample(snap, points['out'])
    level_in = _point_sample(snap, points['level'])
    if not all(isinstance(sample, dict) and sample
               for sample in (primary, backup, out, level_in)):
        return None
    if _good_finite(primary):
        expected, source = primary, 'primary'
    else:
        expected, source = backup, 'backup'
    # The selector's non-finite merge: a selected value that is not
    # finite serves Bad(DeviceFault) rather than the source's stamp.
    expected_quality = _quality_key(expected.get('quality'))
    expected_value = _float_of(expected)
    if not (isinstance(expected_value, (int, float))
            and math.isfinite(expected_value)):
        expected_quality = 'bad:device_fault'
    if _float_of(out) != expected_value \
            or _quality_key(out.get('quality')) != expected_quality:
        return 'out serves ' + json.dumps(out, sort_keys=True) \
            + ' while the ' + source + ' is the selected source (' \
            + json.dumps(expected, sort_keys=True)[:200] + ')'
    demand = _point_value(snap, points['demand'])
    if not _good_finite(level_in) and demand is not None \
            and demand != on_bad:
        return 'demand ' + json.dumps(demand) \
            + ' controls on an untrusted level instead of the ' \
            'declared on_bad_demand ' + json.dumps(on_bad)
    return None


def _await_journal(ctx, base, floor, point, want_to):
    """The first point_changed entry on `point` landing `want_to` above
    the run's journal floor, polled until it lands or the deadline
    passes — the durable record of one named transition."""
    def found():
        try:
            _, body = http_json('GET',
                                base + '/journal?since=' + str(floor))
        except Exception:
            return None
        for entry in _journal_list(body):
            change = (entry.get('event') or {}).get('point_changed')
            if isinstance(change, dict) and change.get('point') == point \
                    and _literal_bool(change.get('to')) is want_to:
                return entry
        return None
    return wait_for(found, time.monotonic() + FALLBACK_DEADLINE,
                    interval=POLL_INTERVAL)


def scenario_unavailable_fallback(ctx):
    """Inject a backup-only then an all-source level fault: the
    backup_unhealthy annunciation asserts with no failover transition,
    the all-bad state serves Bad on the failover output and the
    declared on_bad_demand on the chain — transitions journaled — and
    clearing in reverse order resumes primary control."""
    case = Case('unavailable-fallback',
                'All level sources bad engages the declared fallback',
                'a non-Good fault on the backup level input alone '
                'asserts the failover-select\'s backup_unhealthy while '
                'the healthy primary keeps serving and no failover '
                'transition journals; faulting the primary too leaves '
                'no trustworthy source — the select\'s output serves '
                'Bad and the threshold chain emits its declared '
                'on_bad_demand with the alarmed engagement journaled; '
                'clearing the faults in reverse order resumes primary '
                'control and drops the annunciation through the named '
                'transitions with no unintended output step')
    injected = []
    violations = []
    last = {}
    try:
        # Self-contained on either role layout, like field-fault:
        # whichever peer reports settled active is the observed
        # surface, and every leg is reversible field-side state.
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        base = ctx[active]
        if ctx.get('plant') is None:
            return case.finish('inconclusive',
                               'the run publishes no plant endpoint')
        case.observe('observing ' + active + ' (' + base + ')')

        # The leg's contract out of the served wiring: the
        # failover-select's bound ports and the threshold-chain's
        # delivered level and demand plus its declared on_bad_demand —
        # the model is the contract, so no point is addressed by
        # convention. A model without the optional standby-health port
        # declares no annunciation for this leg to check.
        snap = _snapshot(ctx, base)
        sel_name, sel = _descriptor_ports(
            snap, 'failover-select',
            ('primary', 'backup', 'out', 'backup_active',
             'backup_unhealthy'))
        chain_name, chain = _descriptor_ports(
            snap, 'threshold-chain', ('level', 'demand'))
        on_bad = _parameter_value(snap, chain_name, 'on_bad_demand') \
            if chain_name else None
        # The field census and the fault commands ride the shipped
        # dcs-plant-ctl: neither takes the writer claim, so they run
        # beside the field owner's standing claim exactly as the raw
        # requests did.
        census = _field_inputs(ctx)
        ref = save_evidence(
            ctx['evidence_dir'], 'unavailable-fallback-layout.json',
            {'select': {'name': sel_name, 'ports': sel},
             'chain': {'name': chain_name, 'ports': chain,
                       'on_bad_demand': on_bad},
             'field_inputs': sorted(census)})
        case.evidence('file', ref, 'the served level-path wiring and '
                      'the declared on_bad_demand')
        if sel_name is None or chain_name is None or on_bad is None:
            return case.finish(
                'inconclusive', 'the rig model does not serve the '
                'fallback leg: select=' + str(sel_name) + ' chain='
                + str(chain_name) + ' on_bad_demand=' + str(on_bad))
        points = {'primary': sel['primary'], 'backup': sel['backup'],
                  'out': sel['out'], 'active': sel['backup_active'],
                  'unhealthy': sel['backup_unhealthy'],
                  'level': chain['level'], 'demand': chain['demand']}
        absent = [name for name in ('primary', 'backup')
                  if points[name] not in census]
        if absent:
            return case.finish(
                'inconclusive', 'the bound level sources are not '
                'plant-served field inputs: ' + ', '.join(absent))
        case.observe('level path: primary ' + str(points['primary'])
                     + ', backup ' + str(points['backup']) + ' -> '
                     + str(sel_name) + '.out ' + str(points['out'])
                     + ' -> ' + str(chain_name) + '.demand '
                     + str(points['demand']) + ', declared '
                     'on_bad_demand ' + json.dumps(on_bad))

        def poll():
            """One observation: the served snapshot in, the declared-
            rule check out — every leg polls through here so a step
            outside the selection/fallback rules is recorded against
            the leg that served it."""
            snap = _try_snapshot(ctx, base)
            if snap is None:
                return None
            last['snap'] = snap
            violation = _fallback_violation(snap, points, on_bad)
            if violation:
                violations.append({'tick': snap.get('tick'),
                                   'violation': violation})
            return snap

        # The settled healthy baseline: both sources Good and finite,
        # the primary serving, no annunciation standing — the state
        # the legs leave the rig in, and the only state a backup-only
        # leg can be attributed from.
        def healthy():
            snap = poll()
            if snap is None:
                return None
            if _good_finite(_point_sample(snap, points['primary'])) \
                    and _good_finite(_point_sample(snap,
                                                 points['backup'])) \
                    and _point_value(snap, points['active']) is False \
                    and _point_value(snap, points['unhealthy']) \
                    is False:
                return snap
            return None

        def leg_evidence(name, extra):
            """One leg's evidence record: the last observed samples on
            the leg's points plus the leg's own payload."""
            payload = {'leg': name,
                       'samples': {key: _point_sample(
                           last.get('snap') or {}, points[key])
                           for key in points}}
            payload.update(extra)
            ref = save_evidence(ctx['evidence_dir'],
                                'unavailable-fallback-' + name + '.json',
                                payload)
            case.evidence('file', ref, 'the ' + name + ' leg')

        def unintended():
            """The first recorded rule violation's failure record, or
            None — an output step the declared rules never called
            for."""
            if violations:
                return case.finish(
                    'failed', 'an unintended output step at tick '
                    + str(violations[0]['tick']) + ': '
                    + violations[0]['violation'])
            return None

        if wait_for(healthy, time.monotonic() + FALLBACK_DEADLINE,
                    interval=POLL_INTERVAL) is None:
            return case.finish('inconclusive', 'no healthy settled '
                               'baseline: both level sources Good with '
                               'the primary serving never presented')
        _, body = http_json('GET', base + '/journal?since=0')
        floor = max((entry.get('seq') or 0
                     for entry in _journal_list(body)), default=0)

        # Leg 1 — the backup alone goes untrusted: the standby-health
        # report asserts while the healthy primary keeps the field —
        # the loss of the unused leg annunciated before it is needed —
        # and no failover transition may journal.
        verdict = _plant_ctl(ctx, 'fault', str(points['backup']),
                             'bad:device_fault')
        if verdict.get('result') != 'done':
            return case.finish('failed', 'inject_fault on the backup '
                               'point refused: '
                               + json.dumps(verdict)[:300])
        injected.append(points['backup'])
        moved = []

        def annunciated():
            snap = poll()
            if snap is None:
                return None
            if _point_value(snap, points['active']) is True:
                moved.append(snap.get('tick'))
            if _point_value(snap, points['unhealthy']) is True:
                return snap
            return None

        hit = wait_for(annunciated,
                       time.monotonic() + FALLBACK_DEADLINE,
                       interval=POLL_INTERVAL)
        _, body = http_json('GET',
                            base + '/journal?since=' + str(floor))
        leg_transitions = _journal_transitions(body, points['active'])
        leg_evidence('backup-degraded',
                     {'selection_moves': moved,
                      'failover_transitions': leg_transitions})
        if hit is None:
            detail = 'a backup-only fault never asserted the ' \
                     'backup_unhealthy annunciation: last served ' \
                     + json.dumps(_point_sample(last.get('snap') or {},
                                                points['unhealthy']))[:300]
            if violations:
                detail = 'an unintended output step: ' \
                    + violations[0]['violation']
            return case.finish('failed', detail)
        if moved:
            return case.finish('failed', 'a backup-only fault moved '
                               'the selection — backup_active asserted '
                               'at tick ' + str(moved[0]))
        if leg_transitions:
            return case.finish('failed', 'a backup-only fault '
                               'journaled a failover transition: '
                               + json.dumps(leg_transitions)[:300])
        if _await_journal(ctx, base, floor, points['unhealthy'],
                          True) is None:
            return case.finish('failed', 'the backup_unhealthy '
                               'annunciation never journaled')
        case.observe('backup-only fault: backup_unhealthy asserted at '
                     'tick ' + str(hit.get('tick'))
                     + ', the primary kept serving, no failover '
                     'transition journaled')
        failed = unintended()
        if failed:
            return failed

        # Leg 2 — the primary joins it: no trustworthy source remains,
        # so `out` serves the failed backup verbatim — Bad — and the
        # chain emits the declared on_bad_demand instead of controlling
        # on the untrusted measurement; the engagement is the alarmed
        # transition the durable record carries.
        verdict = _plant_ctl(ctx, 'fault', str(points['primary']),
                             'bad:device_fault')
        if verdict.get('result') != 'done':
            return case.finish('failed', 'inject_fault on the primary '
                               'point refused: '
                               + json.dumps(verdict)[:300])
        injected.append(points['primary'])

        def all_bad():
            snap = poll()
            if snap is None:
                return None
            if _point_value(snap, points['active']) is True \
                    and not _good_finite(
                        _point_sample(snap, points['out'])) \
                    and not _good_finite(
                        _point_sample(snap, points['level'])) \
                    and _point_value(snap, points['demand']) == on_bad:
                return snap
            return None

        hit = wait_for(all_bad, time.monotonic() + FALLBACK_DEADLINE,
                       interval=POLL_INTERVAL)
        leg_evidence('all-bad', {'on_bad_demand': on_bad})
        if hit is None:
            if violations:
                return case.finish('failed', 'an unintended output '
                                   'step: ' + violations[0]['violation'])
            return case.finish('failed', 'the all-bad state never '
                               'engaged the declared fallback: last '
                               'served out '
                               + json.dumps(_point_sample(
                                   last.get('snap') or {},
                                   points['out']))[:200] + ', demand '
                               + json.dumps(_point_value(
                                   last.get('snap') or {},
                                   points['demand'])))
        out_quality = _quality_key(
            _point_sample(hit, points['out']).get('quality'))
        if not out_quality.startswith('bad:'):
            return case.finish('failed', 'the failover output did not '
                               'serve Bad while every source was '
                               'untrusted: ' + out_quality)
        if _await_journal(ctx, base, floor, points['active'],
                          True) is None:
            return case.finish('failed', 'the alarmed backup_active '
                               'engagement never journaled')
        case.observe('all sources bad: out serves ' + out_quality
                     + ', demand ' + json.dumps(on_bad)
                     + ' (the declared on_bad_demand), the engagement '
                     'journaled')
        failed = unintended()
        if failed:
            return failed

        # Leg 3 — reverse-order clearing: the primary recovers first.
        # The stateless selector re-selects it the scan its sample
        # turns Good — backup_active drops through the named
        # transition — while the still-bad backup keeps the
        # annunciation standing.
        verdict = _plant_ctl(ctx, 'clear-fault', str(points['primary']))
        if verdict.get('result') != 'done':
            return case.finish('failed', 'clear_fault on the primary '
                               'point refused: '
                               + json.dumps(verdict)[:300])
        injected.remove(points['primary'])

        def primary_back():
            snap = poll()
            if snap is None:
                return None
            if _point_value(snap, points['active']) is False \
                    and _good_finite(
                        _point_sample(snap, points['out'])):
                return snap
            return None

        hit = wait_for(primary_back,
                       time.monotonic() + FALLBACK_DEADLINE,
                       interval=POLL_INTERVAL)
        leg_evidence('primary-recovered', {})
        if hit is None:
            if violations:
                return case.finish('failed', 'an unintended output '
                                   'step: ' + violations[0]['violation'])
            return case.finish('failed', 'the recovered primary never '
                               'resumed control: last served out '
                               + json.dumps(_point_sample(
                                   last.get('snap') or {},
                                   points['out']))[:300])
        if _point_value(hit, points['unhealthy']) is not True:
            return case.finish('failed', 'the backup_unhealthy '
                               'annunciation dropped while the backup '
                               'stayed bad')
        if _await_journal(ctx, base, floor, points['active'],
                          False) is None:
            return case.finish('failed', 'the return to primary '
                               'control never journaled')
        case.observe('primary recovered: backup_active cleared at '
                     'tick ' + str(hit.get('tick'))
                     + ', the primary serves again with the '
                     'annunciation still standing')
        failed = unintended()
        if failed:
            return failed

        # Leg 4 — the backup's clear drops the annunciation through
        # the named transition, leaving the rig on the healthy
        # baseline the legs started from.
        verdict = _plant_ctl(ctx, 'clear-fault', str(points['backup']))
        if verdict.get('result') != 'done':
            return case.finish('failed', 'clear_fault on the backup '
                               'point refused: '
                               + json.dumps(verdict)[:300])
        injected.remove(points['backup'])

        def annunciation_clear():
            snap = poll()
            if snap is None:
                return None
            if _point_value(snap, points['unhealthy']) is False:
                return snap
            return None

        hit = wait_for(annunciation_clear,
                       time.monotonic() + FALLBACK_DEADLINE,
                       interval=POLL_INTERVAL)
        leg_evidence('cleared', {'violations': violations})
        if hit is None:
            return case.finish('failed', 'clearing the backup never '
                               'cleared the annunciation: last served '
                               + json.dumps(_point_sample(
                                   last.get('snap') or {},
                                   points['unhealthy']))[:300])
        if _await_journal(ctx, base, floor, points['unhealthy'],
                          False) is None:
            return case.finish('failed', 'the annunciation\'s clear '
                               'never journaled')
        failed = unintended()
        if failed:
            return failed
        case.observe('backup recovered: the annunciation cleared and '
                     'journaled; the pair is back on the healthy '
                     'baseline')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        # The injected level points are the run's shared field: a case
        # that leaves them faulted poisons every later scenario.
        for point in injected:
            try:
                _plant_ctl(ctx, 'clear-fault', str(point))
            except Exception:
                pass
