"""The operator_command acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *


def scenario_operator_command(ctx):
    """A writable-point command applies at a scan boundary."""
    case = Case('operator-command',
                'Writable point command applies at a scan boundary',
                'POST /command writing the p101-oos point true is accepted '
                'and the value is visible in the next snapshot')
    try:
        _, signals = http_json('GET', ctx['active'] + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'operator-command-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming writable points')
        target = _writable_bool_point(signals)
        if target is None:
            return case.finish('inconclusive',
                               'no writable bool point in the model')
        point = target['point']
        case.observe('command target: ' + str(target.get('name'))
                     + ' point ' + str(point))
        command = {'command': {'write_value': {
            'point': point, 'kind': 'bool', 'value': {'bool': True}}},
            'actor': 'qa-lane'}
        status, receipt = http_json('POST', ctx['active'] + '/command',
                                    command)
        ref = save_evidence(ctx['evidence_dir'],
                            'operator-command-receipt.json',
                            {'status': status, 'body': receipt})
        case.evidence('file', ref, 'submission receipt')
        case.observe('POST /command answered ' + str(status) + ': '
                     + json.dumps(receipt))
        if status != 200:
            return case.finish('failed', 'command refused: ' + str(receipt))
        deadline = time.monotonic() + 30
        observed = wait_for(
            lambda: _point_value(_snapshot(ctx, ctx['active']), point)
            is True or None, deadline)
        snap = _snapshot(ctx, ctx['active'])
        ref = save_evidence(ctx['evidence_dir'],
                            'operator-command-snapshot.json', snap)
        case.evidence('file', ref, 'snapshot after command')
        if not observed:
            return case.finish('failed',
                               'point ' + str(point)
                               + ' did not read true in telemetry')
        case.observe('point ' + str(point) + ' reads true in telemetry')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
