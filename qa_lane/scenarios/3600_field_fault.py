"""The field_fault acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *


# --------------------------------------------------------------------
# The field-fault schedule (WW-OPS-003's signal-confidence clause and
# WW-FND-002's remote-I/O degradation path): the lane drives the same
# per-point failure surface the hardware lane will grade for channel
# faults — InjectFault/ClearFault over the plant's newline-JSON
# protocol on the run's published plant port. A quality fault must
# present the point degraded — the substituted quality stamped on the
# stored field value, never a silently healthy last-known — and
# clearing it must restore the field value at Good; an error fault must
# surface through the driver's IoError path into io_health (the
# per-direction counters, last_error with tick and direction) while the
# scan continues and no role change follows — field faults are not
# peer loss.

FAULT_DEADLINE = 30   # bound on one injection surfacing or a clear
FAULT_PROBE = 1.0     # plant-step window between stability probes


def scenario_field_fault(ctx):
    """Injected field-point faults degrade through the monitor and
    clear — never masquerading as healthy, never costing the active
    its role."""
    case = Case('field-fault',
                'Injected field faults degrade, recover, and keep role',
                'a quality fault injected on a field In point serves '
                'the point with the substituted quality stamped — '
                'never a silently Good value — clearing it restores '
                'the simulated field value at Good quality, and a '
                'disconnected-class fault on a second field point '
                'surfaces on io_health (failed_reads, last_error with '
                'tick and direction) while the scan continues and no '
                'role change follows')
    injected = []
    try:
        # Self-contained on either role layout, like evidence-capture:
        # whichever peer reports settled active is the observed surface.
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        base = ctx[active]
        if ctx.get('plant') is None:
            return case.finish('inconclusive',
                               'the run publishes no plant endpoint')
        case.observe('plant tool at ' + str(ctx['plant'])
                     + '; observing ' + active + ' (' + base + ')')

        # Fault targets must be points the field itself holds still:
        # only a stable stored value can prove the clear restored the
        # field value rather than a moved one. Two list_points probes
        # straddling a few plant steps find them, and each must already
        # read Good on the monitor — a forced or degraded point cannot
        # evidence a fault it would mask. The field ops ride the
        # shipped dcs-plant-ctl: read, list, and the fault commands need
        # no writer claim, so they run beside the field owner's
        # standing claim exactly as the raw requests did.
        first = _field_inputs(ctx)
        time.sleep(FAULT_PROBE)
        second = _field_inputs(ctx)
        snap = _snapshot(ctx, base)
        served_good = {
            entry.get('point') for entry in snap.get('points', [])
            if _quality_key((entry.get('sample') or {}).get('quality'))
            == 'good'}
        stable = sorted(
            point for point, info in first.items()
            if point in second and point in served_good
            and _quality_key((info.get('sample') or {}).get('quality'))
            == 'good'
            and _quality_key((second[point].get('sample') or {})
                             .get('quality')) == 'good'
            and (info.get('sample') or {}).get('value')
            == (second[point].get('sample') or {}).get('value'))
        ref = save_evidence(ctx['evidence_dir'],
                            'field-fault-points.json',
                            {'served': sorted(second), 'stable': stable})
        case.evidence('file', ref, 'the list_points census and the '
                      'stability probe')
        if not stable:
            return case.finish('inconclusive',
                               'no stable healthy field In point to '
                               'fault')
        quality_point = stable[0]
        alternates = [point for point in stable if point != quality_point]
        if not alternates:
            alternates = sorted(point for point in second
                                if point != quality_point)
        if not alternates:
            return case.finish('inconclusive',
                               'no second field In point to fault')
        error_point = alternates[0]
        case.observe('fault targets: quality point '
                     + str(quality_point) + ', error point '
                     + str(error_point))
        last = {}

        # Leg 1: a quality fault substitutes the served quality, leaving
        # the stored field value — the bad-data-confidence clause's
        # "degraded, never silently healthy" half.
        field_value = _plant_read(ctx, quality_point).get('value')
        verdict = _plant_ctl(ctx, 'fault', str(quality_point),
                             'bad:device_fault')
        if verdict.get('result') != 'done':
            return case.finish('failed', 'inject_fault refused: '
                               + json.dumps(verdict)[:300])
        injected.append(quality_point)

        def degraded():
            snap = _try_snapshot(ctx, base)
            if snap is None:
                return None
            last['snap'] = snap
            sample = _point_sample(snap, quality_point)
            if sample and _quality_key(sample.get('quality')) \
                    == 'bad:device_fault':
                return sample
            return None

        hit = wait_for(degraded, time.monotonic() + FAULT_DEADLINE)
        ref = save_evidence(
            ctx['evidence_dir'], 'field-fault-degraded.json',
            {'point': quality_point,
             'sample': hit or _point_sample(last.get('snap') or {},
                                          quality_point)})
        case.evidence('file', ref, 'the faulted point under injection')
        if not hit:
            return case.finish(
                'failed', 'the quality fault on point '
                + str(quality_point) + ' never surfaced: the served '
                'sample stayed '
                + json.dumps(_point_sample(last.get('snap') or {},
                                           quality_point))[:300])
        if hit.get('value') != field_value:
            return case.finish(
                'failed', 'the degraded sample replaced the field '
                'value ' + json.dumps(field_value) + ' with '
                + json.dumps(hit.get('value')))
        case.observe('point ' + str(quality_point)
                     + ' serves bad:device_fault over the stored field '
                     'value ' + json.dumps(field_value))
        if _settled_active(ctx) != active:
            return case.finish('failed', 'a quality fault moved the '
                               'active role — a field fault is not '
                               'peer loss')

        verdict = _plant_ctl(ctx, 'clear-fault', str(quality_point))
        if verdict.get('result') != 'done':
            return case.finish('failed', 'clear_fault refused: '
                               + json.dumps(verdict)[:300])
        injected.remove(quality_point)

        def recovered():
            snap = _try_snapshot(ctx, base)
            if snap is None:
                return None
            last['snap'] = snap
            sample = _point_sample(snap, quality_point)
            if not sample or _quality_key(sample.get('quality')) \
                    != 'good':
                return None
            try:
                field = _plant_read(ctx, quality_point)
            except Exception:
                return None
            if sample.get('value') == field.get('value'):
                return sample
            return None

        hit = wait_for(recovered, time.monotonic() + FAULT_DEADLINE)
        ref = save_evidence(
            ctx['evidence_dir'], 'field-fault-recovered.json',
            {'point': quality_point,
             'sample': hit or _point_sample(last.get('snap') or {},
                                          quality_point)})
        case.evidence('file', ref, 'the point after clearing')
        if not hit:
            return case.finish(
                'failed', 'clearing the fault on point '
                + str(quality_point) + ' never restored the field '
                'value at Good quality; last served '
                + json.dumps(_point_sample(last.get('snap') or {},
                                           quality_point))[:300])
        case.observe('point ' + str(quality_point)
                     + ' recovered to the field value at Good')

        # Leg 2: an error fault answers the driver's read with an
        # IoError — the remote-I/O degradation path. It must surface on
        # io_health attributed to the point and the in direction, the
        # served sample must degrade rather than pose as healthy
        # last-known, the link must stay up (a point fault is not link
        # loss), the scan must not stall, and the role must not move.
        def healthy_second():
            snap = _try_snapshot(ctx, base)
            if snap is None:
                return None
            last['snap'] = snap
            sample = _point_sample(snap, error_point)
            if sample and _quality_key(sample.get('quality')) \
                    == 'good':
                return snap
            return None

        # The second leg's baseline: the target must read healthy ahead
        # of its injection, so the counters it moves are attributable.
        before = wait_for(healthy_second,
                          time.monotonic() + FAULT_DEADLINE)
        if not before:
            return case.finish(
                'inconclusive', 'the error-fault target point '
                + str(error_point) + ' never read healthy ahead of '
                'injection')
        health0 = before.get('io_health') or {}
        tick0 = before.get('tick') or 0
        verdict = _plant_ctl(ctx, 'fault', str(error_point),
                             'disconnected')
        if verdict.get('result') != 'done':
            return case.finish('failed', 'inject_fault refused: '
                               + json.dumps(verdict)[:300])
        injected.append(error_point)

        def surfaced():
            snap = _try_snapshot(ctx, base)
            if snap is None:
                return None
            last['snap'] = snap
            health = snap.get('io_health') or {}
            fault = health.get('last_error') or {}
            if (health.get('failed_reads') or 0) \
                    > (health0.get('failed_reads') or 0) \
                    and fault.get('point') == error_point \
                    and fault.get('direction') == 'in' \
                    and fault.get('error') \
                    == {'disconnected': error_point}:
                return snap
            return None

        snap = wait_for(surfaced, time.monotonic() + FAULT_DEADLINE)
        ref = save_evidence(
            ctx['evidence_dir'], 'field-fault-io-health.json',
            {'point': error_point,
             'io_health': (last.get('snap') or {}).get('io_health'),
             'sample': _point_sample(last.get('snap') or {},
                                     error_point)})
        case.evidence('file', ref, 'io_health under the error fault')
        if not snap:
            return case.finish(
                'failed', 'the error fault on point '
                + str(error_point) + ' never surfaced on io_health: '
                + json.dumps((last.get('snap') or {})
                             .get('io_health'))[:400])
        health = snap.get('io_health') or {}
        fault = health.get('last_error') or {}
        if (fault.get('tick') or 0) < tick0:
            return case.finish('failed', 'last_error predates the '
                               'injection: ' + json.dumps(fault)[:300])
        if (health.get('failed_writes') or 0) \
                != (health0.get('failed_writes') or 0):
            return case.finish('failed', 'an in-point read fault '
                               'moved the out-direction counter: '
                               + json.dumps(health)[:400])
        link = (health.get('driver') or {}).get('link')
        if link != 'connected':
            return case.finish('failed', 'a point fault presented as '
                               'link loss: ' + str(link))
        sample = _point_sample(snap, error_point)
        if _quality_key((sample or {}).get('quality')) \
                != 'bad:communication_fault':
            return case.finish(
                'failed', 'the error-faulted point did not serve '
                'degraded: ' + json.dumps(sample)[:300])
        if (snap.get('tick') or 0) <= tick0:
            return case.finish('failed', 'the scan did not advance '
                               'past the injection')
        case.observe('io_health attributes point ' + str(error_point)
                     + ': failed_reads '
                     + str(health0.get('failed_reads') or 0) + ' -> '
                     + str(health.get('failed_reads'))
                     + ', last_error ' + json.dumps(fault)
                     + ', link ' + str(link))
        grown = wait_for(
            lambda: (s.get('tick', 0) > snap.get('tick', 0) and s
                     or None)
            if (s := _try_snapshot(ctx, base)) else None,
            time.monotonic() + FAULT_DEADLINE)
        if not grown:
            return case.finish('failed', 'the scan stalled while the '
                               'error fault stood')
        roles = {}
        for name in ('active', 'standby'):
            try:
                roles[name] = _role(ctx, ctx[name])
            except Exception as exc:
                roles[name] = {'unreachable': str(exc)[:200]}
        ref = save_evidence(ctx['evidence_dir'],
                            'field-fault-roles.json', roles)
        case.evidence('file', ref, 'roles while the error fault stands')
        if _settled_active(ctx) != active:
            return case.finish('failed', 'an error fault moved the '
                               'active role — a field fault is not '
                               'peer loss: ' + json.dumps(roles)[:300])
        case.observe('scan advancing (tick ' + str(snap.get('tick'))
                     + ' -> ' + str(grown.get('tick'))
                     + '), ' + active + ' still active under the '
                     'error fault')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        # The injected points are the run's shared field: a case that
        # leaves them faulted poisons every later scenario.
        for point in injected:
            try:
                _plant_ctl(ctx, 'clear-fault', str(point))
            except Exception:
                pass
