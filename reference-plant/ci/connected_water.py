#!/usr/bin/env python3
"""Connected water-area acceptance, using the existing driven consumer harness.

No plant-specific JavaScript or controller code. Reads the emitted model's
standard signal identities; records deterministic process and receipt evidence.
Use --emit-scenario to run the same exercise under ci/consumers.py schedules.
"""
import argparse
import json
from pathlib import Path
import sys
import simulate


def install_authoritative_capture():
    """Extend the existing harness through its between-legs hook.

    Include every point sample and component state, not just the asserted
    readings. Receipt mint identities differ across boots; compare complete
    command/outcome/actor payloads here, while existing restart/pair audits
    separately prove identity preservation and once-only adoption.
    """
    original = simulate.run_legs
    def captured(monitor, client, legs, between_legs=None):
        records = []
        def boundary(index):
            snapshot = simulate.http(f"{monitor}/snapshot")
            receipts = simulate.http(f"{monitor}/receipts")
            records.append({
                'points': snapshot['points'], 'components': snapshot['components'],
                'command_outcomes': [{k: v for k, v in receipt.items() if k != 'submission'} for receipt in receipts],
            })
            if between_legs:
                between_legs(index)
        entries, failures = original(monitor, client, legs, between_legs=boundary)
        for entry, record in zip(entries, records):
            entry['authoritative'] = record
        return entries, failures
    simulate.run_legs = captured


def scenario(model):
    points = {s['name']: s['source'] for s in model['signals']}
    def p(name):
        return points[name]
    def write(name, kind, value, reason=None):
        body = {'actor': 'water-acceptance', 'command': {'write_value': {
            'point': p(name), 'kind': kind, 'value': {kind: value}}}}
        if reason:
            body['reason'] = reason
        return body
    def fault(name, quality):
        return {'op': 'inject_fault', 'point': p(name), 'fault': {'quality': quality}}
    def clear(name):
        return {'op': 'clear_fault', 'point': p(name)}
    legs = []
    def leg(name, scans, expect, commands=(), receipts=None, plant=()):
        segment = 0
        while scans > 256:
            segment += 1
            legs.append({'name': f'{name}-segment-{segment}', 'scans': 256, 'expect': {},
                'commands': list(commands), 'expect_receipts': receipts or ['accepted'] * len(commands), 'plant': list(plant)})
            scans -= 256
            commands, receipts, plant = (), None, ()
        legs.append({'name': name, 'scans': scans,
            'expect': {str(p(k)): v for k, v in expect.items()},
            'commands': list(commands), 'expect_receipts': receipts or ['accepted'] * len(commands), 'plant': list(plant)})
    leg('normal-pump-transfer', 100, {'balance-inflow': {'min': 60}, 'balance-level': {'min': 1, 'max': 4}})
    leg('setpoint-change', 100, {'lic201-setpoint': {'float': 2.8}, 'lv201-applied': {'min': 0, 'max': 100}}, [write('lic201-setpoint', 'float', 2.8)])
    leg('setpoint-admission-bounds', 0, {'lic201-setpoint': {'float': 2.8}}, [write('lic201-setpoint', 'float', 4.0)], ['point_out_of_range'])
    leg('closed-outlet-blocks-demand', 40, {'xv201-feedback': {'bool': False}, 'lv201-applied': {'float': 0.0}, 'lv201-tripped': {'bool': True}, 'physical-outlet-flow': {'float': 0.0}, 'outlet-flow': {'min': 0, 'max': 0.001}}, [write('xv201-request', 'bool', False)])
    leg('manual-request-remains-protected', 40, {'lv201-manual': {'float': 30.0}, 'lv201-applied': {'float': 0.0}}, [write('lv201-mode', 'bool', True), write('lv201-manual', 'float', 30.0)])
    leg('open-outlet-and-manual-flow', 60, {'lv201-applied': {'float': 30.0}, 'outlet-flow': {'min': 42, 'max': 44}}, [write('xv201-request', 'bool', True)])
    leg('manual-admission-bounds', 0, {'lv201-manual': {'float': 30.0}}, [write('lv201-manual', 'float', 101.0)], ['point_out_of_range'])
    leg('failed-position-feedback', 60, {'lv201-fault': {'bool': True}, 'lv201-alarm': {'bool': True}, 'lv201-unacknowledged': {'bool': True}, 'outlet-flow': {'min': 42, 'max': 44}}, plant=[fault('lv201-feedback', {'bad': 'device_fault'})])
    leg('ack-does-not-reset-fault', 4, {'lv201-fault': {'bool': True}, 'lv201-unacknowledged': {'bool': False}}, [write('lv201-ack', 'bool', True)])
    leg('feedback-recovery', 80, {'lv201-fault': {'bool': False}, 'lv201-alarm': {'bool': False}}, [write('lv201-ack', 'bool', False)], plant=[clear('lv201-feedback')])
    # Bad measurements trip the actuator even when its operator selected manual.
    leg('bad-measurement-protects-manual', 30, {'lv201-applied': {'float': 0.0}, 'lv201-tripped': {'bool': True}, 'outlet-flow': {'max': 0.5}}, plant=[fault('balance-level', {'bad': 'device_fault'})])
    leg('stale-measurement-protects-manual', 30, {'lv201-applied': {'float': 0.0}, 'lv201-tripped': {'bool': True}, 'outlet-flow': {'max': 0.5}}, plant=[fault('balance-level', {'uncertain': 'stale'})])
    leg('measurement-recovery', 60, {'lv201-tripped': {'bool': False}, 'lv201-applied': {'float': 30.0}}, plant=[clear('balance-level')])
    leg('failed-isolation-feedback', 40, {'xv201-fault': {'bool': True}, 'xv201-alarm': {'bool': True}, 'lv201-tripped': {'bool': True}, 'lv201-applied': {'float': 0.0}, 'outlet-flow': {'max': 0.5}}, plant=[fault('xv201-feedback', {'bad': 'device_fault'})])
    leg('isolation-alarm-ack', 4, {'xv201-fault': {'bool': True}, 'xv201-unacknowledged': {'bool': False}}, [write('xv201-ack', 'bool', True)])
    leg('isolation-feedback-recovery', 80, {'xv201-fault': {'bool': False}, 'xv201-alarm': {'bool': False}, 'lv201-tripped': {'bool': False}, 'outlet-flow': {'min': 42, 'max': 44}}, [write('xv201-ack', 'bool', False)], plant=[clear('xv201-feedback')])
    leg('return-to-automatic', 200, {'lv201-mode': {'bool': False}, 'lv201-applied': {'min': 0, 'max': 100}}, [write('lv201-mode', 'bool', False)])
    # A high process warning remains a notification, not an automatic trip.
    leg('storage-warning', 400, {'lt201-alarm': {'bool': True}, 'lt201-unacknowledged': {'bool': True}, 'balance-level': {'min': 4.2, 'max': 5.0}}, [write('xv201-request', 'bool', False)])
    leg('shelving-requires-reason', 0, {'lt201-shelved': {'bool': False}}, [write('lt201-shelve', 'bool', True)], ['reason_required'])
    leg('bounded-warning-shelving', 4, {'lt201-shelved': {'bool': True}, 'lt201-alarm': {'bool': True}}, [write('lt201-shelve', 'bool', True, 'Inspect outlet during simulated maintenance')])
    leg('warning-acknowledgment', 4, {'lt201-unacknowledged': {'bool': False}}, [write('lt201-ack', 'bool', True), write('lt201-shelve', 'bool', False, 'Release simulated warning shelving request')])
    leg('shelving-expiry', 305, {'lt201-shelved': {'bool': False}})
    leg('warning-recovery', 500, {'balance-level': {'min': 0, 'max': 4.2}, 'lt201-alarm': {'bool': False}}, [write('xv201-request', 'bool', True), write('lt201-ack', 'bool', False)])
    return {'dt': 0.2, 'legs': legs}


def complete_workflows(args, exercise, reference):
    """Reuse the consumer, durable-restart and declared-pair rigs."""
    import consumers
    import restart
    import pair  # restart installs the existing legs directory
    failures = []
    point = json.loads(Path(args.model).read_text())['io_points'][0]['id']
    expected = simulate.stable_digest(reference)
    for schedule in consumers.SCHEDULES:
        print('connected-water: consumer ' + schedule, file=sys.stderr, flush=True)
        args.schedule = schedule
        entries, found = consumers.run_schedule(args, exercise, point)
        failures.extend(found)
        if simulate.stable_digest(entries) != expected:
            failures.append(schedule + ': authoritative output/receipt digest changed')
    print('connected-water: durable restart', file=sys.stderr, flush=True)
    entries, evidence, found = restart.interrupted_pass(args, exercise,
        {'state_file': 'state.json', 'journal_file': 'journal.jsonl', 'history_file': 'history.jsonl'}, None)
    failures.extend(found)
    if simulate.stable_digest(entries) != expected:
        failures.append('restart: authoritative output/receipt digest changed')
    args.dt = exercise['dt']
    # PairRig's fingerprint is used for persistence audits; it is obtained from
    # the generic runtime, never recomputed by customer code.
    with simulate.driven_rig(args.plant_server, args.controller, args.model, args.dynamics, args.dt) as (_, monitor):
        fingerprint = simulate.http(monitor + '/checkpoint')['model_fingerprint']
    duty = {'name': 'water-a', 'state_file': 'state.json', 'journal_file': 'journal.jsonl', 'history_file': 'history.jsonl'}
    standby = dict(duty, name='water-b', standby='water-a:8080')
    manifest = {'model': {'fingerprint': format(fingerprint, '016x')}}
    print('connected-water: compatible takeover', file=sys.stderr, flush=True)
    rig = pair.launch_pair(args, (manifest, duty, standby))
    owner, tracked = rig.duty_url, rig.standby_url
    try:
        rig.converge(failures)
        for index, leg in enumerate(exercise['legs']):
            for request in leg.get('plant', []):
                response = rig.plant_io.request(request)
                if response.get('result') == 'error' or 'error' in response:
                    failures.append('pair: plant fault refused: ' + str(response))
            for body, expected_outcome in zip(leg.get('commands', []), leg.get('expect_receipts', [])):
                _, receipt = pair.request(owner + '/command', body)
                if simulate.receipt_outcome(receipt) != expected_outcome:
                    failures.append('pair: ' + leg['name'] + ': unexpected receipt ' + str(receipt))
            for _ in range(leg['scans']):
                rig.tick(tracked, owner, failures)
            snapshot = simulate.http(owner + '/snapshot')
            for point_text, expectation in leg['expect'].items():
                mismatch = simulate.check_expectation(leg['name'], int(point_text), expectation,
                    simulate.snapshot_point(snapshot, int(point_text)))
                if mismatch:
                    failures.append('pair: ' + mismatch)
            if index == len(exercise['legs']) // 2:
                rig.switch(owner, tracked, failures, audit_receipts=True)
                owner, tracked = tracked, owner
        if simulate.http(owner + '/receipts') != simulate.http(tracked + '/receipts'):
            failures.append('pair: terminal receipt logs diverged')
    except pair.Abort as error:
        failures.extend(str(arg) for arg in error.args)
    finally:
        rig.close()
    return failures, {'consumer_schedules': consumers.SCHEDULES,
        'authoritative_digest': expected, 'restart': {k: v for k, v in evidence.items() if k != 'final'},
        'takeover': 'all scenario legs, identical peer images and receipts'}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True)
    parser.add_argument('--dynamics')
    parser.add_argument('--controller')
    parser.add_argument('--plant-server')
    parser.add_argument('--emit-scenario', action='store_true')
    parser.add_argument('--extended', action='store_true', help='also prove consumer isolation, restart and takeover')
    parser.add_argument('--evidence', help='write structured acceptance results')
    args = parser.parse_args()
    model = json.loads(Path(args.model).read_text())
    exercise = scenario(model)
    if args.emit_scenario:
        print(json.dumps(exercise, indent=2, sort_keys=True))
        return 0
    for name in ('controller', 'plant_server', 'dynamics'):
        if not getattr(args, name):
            parser.error('--' + name.replace('_', '-') + ' is required')
    install_authoritative_capture()
    with simulate.driven_rig(args.plant_server, args.controller, args.model, args.dynamics, exercise['dt']) as (address, monitor):
        client = simulate.PlantClient(address)
        try:
            entries, failures = simulate.run_legs(monitor, client, exercise['legs'])
            receipts = simulate.http(monitor + '/receipts')
            accepted = sum(outcome == 'accepted' for entry in entries for outcome in entry['receipts'])
            applied = [r for r in receipts if simulate.receipt_outcome(r) == 'applied']
            if len(applied) != accepted or any(simulate.receipt_outcome(r) == 'accepted' for r in receipts):
                failures.append(f'expected {accepted} terminal applied receipts, found {len(applied)}')
        finally:
            client.close()
    evidence = {'trace_scope': 'every point sample/quality, component state and command outcome at every scenario boundary', 'scenario_digest': simulate.stable_digest(entries), 'admitted': accepted, 'applied': len(applied)}
    if args.extended and not failures:
        found, extended = complete_workflows(args, exercise, entries)
        failures.extend(found)
        evidence.update(extended)
    evidence['failures'] = failures
    if args.evidence:
        Path(args.evidence).write_text(json.dumps(evidence, indent=2) + '\n')
    if failures:
        print('\n'.join(failures), file=sys.stderr)
        return 1
    print('connected-water-digest ' + simulate.stable_digest(entries))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
