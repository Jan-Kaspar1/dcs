"""The 3920_managed_state_carryover leg's scenario unit coverage —
the feed fakes and TestCase classes for
scenario_managed_state_carryover. The shared fakes and helpers live
beside each leg's own test module; no shared file is edited.
EXPECTED_CASES pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from qa_lane import report as qa_report
from qa_lane import scenarios

leg = scenarios.scenario_managed_state_carryover.__module__
leg = __import__(leg, fromlist=['x'])

EXPECTED_CASES = frozenset({
    'CarryoverTests.test_registered_in_scenarios',
    'CarryoverTests.test_clean_feed_passes_and_validates',
    'CarryoverTests.test_two_runs_produce_identical_evidence',
    'CarryoverTests.test_no_active_fails',
    'CarryoverTests.test_missing_wiring_is_inconclusive',
    'CarryoverTests.test_unwritable_shelve_is_inconclusive',
    'CarryoverTests.test_switch_outside_bound_fails',
    'CarryoverTests.test_dropped_latch_fails',
    'CarryoverTests.test_gapped_journal_is_nondeterministic',
    'CarryoverTests.test_roles_not_restored_fails',
    'CarryoverHelperTests.test_carry_points_map_names',
    'CarryoverHelperTests.test_missing_signal_is_inconclusive',
    'CarryoverHelperTests.test_demote_refused_fails',
    'CarryoverHelperTests.test_promote_never_succeeding_fails',
})

POINTS = {'lal_shelve': 1011, 'lal_shelved': 1015,
          'contact': 1080, 'alarm': 1083, 'unack': 1084,
          'ack': 1081, 'oos': 302, 'fault_alarm': 1093,
          'fault_unack': 1094, 'fault_oos': 1077}
LAL_NAME = 'lal-managed'
BOUND = 8


def signals_payload(omit=(), unwritable=()):
    names = {'lal-shelve': 'lal_shelve',
             'lal-shelved': 'lal_shelved',
             'p101-moisture': 'contact',
             'p101-moisture-alarm': 'alarm',
             'p101-moisture-unacknowledged': 'unack',
             'p101-moisture-ack': 'ack',
             'p101-oos': 'oos',
             'p101-fault-alarm': 'fault_alarm',
             'p101-fault-unacknowledged': 'fault_unack',
             'p101-fault-out-of-service': 'fault_oos'}
    entries = []
    for name, key in names.items():
        if name in omit:
            continue
        writable = key in ('lal_shelve', 'ack', 'oos') \
            and name not in unwritable
        entries.append({'name': name, 'point': POINTS[key],
                        'direction': 'in' if key in (
                            'lal_shelve', 'ack', 'oos',
                            'contact') else 'out',
                        'value_type': 'bool', 'writable': writable})
    return {'points': entries}


def schema_payload():
    return {'interfaces': [
        {'name': LAL_NAME,
         'interface': {'kind': 'managed-latching-alarm'}}]}


class CarryRig:
    """A scripted monitor pair for the carryover scenario: the
    mid-run shelve countdown, the moisture latch under the consumed
    edge, the OOS level, and one continuous journal across the
    demote/promote rounds. Flags stage each named failure the issue
    calls out."""

    def __init__(self):
        self.tick = 0
        self.seq = 0
        self.shelve_req = False
        self.shelve_elapsed = 0
        self.contact = False
        self.oos = False
        self.latched = False
        self.ack_level = False
        self.alarm = False
        self.journal = []
        self.active = 'active'
        self.roles = None
        self.drop_on_promote = False
        self.journal_gap = False
        self.tick_step = 1
        self.signals = signals_payload()

    def _append(self, event):
        self.seq += 1
        self.journal.append({'seq': self.seq, 'tick': self.tick,
                             'event': event})

    def _changed(self, point, was, now):
        self._append({'point_changed': {
            'point': point, 'from': {'bool': was},
            'to': {'bool': now}}})

    def snapshot(self):
        """One completed scan: the shelve countdown, the latch edge,
        and the OOS level — every transition journaled."""
        self.tick += self.tick_step
        if self.shelve_req:
            self.shelve_elapsed += 1
        else:
            self.shelve_elapsed = 0
        shelved = self.shelve_req and self.shelve_elapsed <= BOUND
        was_shelved = getattr(self, 'shelved', False)
        if shelved != was_shelved:
            self.shelved = shelved
            self._changed(POINTS['lal_shelved'], was_shelved, shelved)
        was_alarm = self.alarm
        self.alarm = self.contact
        if self.alarm != was_alarm:
            self._changed(POINTS['alarm'], was_alarm, self.alarm)
        if self.alarm and not was_alarm and not self.latched:
            self.latched = True
            self._changed(POINTS['unack'], False, True)
        was_oos = getattr(self, 'fault_oos', False)
        if self.oos != was_oos:
            self.fault_oos = self.oos
            self._changed(POINTS['fault_oos'], was_oos, self.oos)
        return {'tick': self.tick,
                'points': [
                    {'point': POINTS['lal_shelve'], 'sample': {
                        'value': {'bool': self.shelve_req}}},
                    {'point': POINTS['lal_shelved'], 'sample': {
                        'value': {'bool': getattr(
                            self, 'shelved', False)}}},
                    {'point': POINTS['contact'], 'sample': {
                        'value': {'bool': self.contact}}},
                    {'point': POINTS['alarm'], 'sample': {
                        'value': {'bool': self.alarm}}},
                    {'point': POINTS['unack'], 'sample': {
                        'value': {'bool': self.latched}}},
                    {'point': POINTS['ack'], 'sample': {
                        'value': {'bool': self.ack_level}}},
                    {'point': POINTS['oos'], 'sample': {
                        'value': {'bool': self.oos}}},
                    {'point': POINTS['fault_alarm'], 'sample': {
                        'value': {'bool': False}}},
                    {'point': POINTS['fault_unack'], 'sample': {
                        'value': {'bool': False}}},
                    {'point': POINTS['fault_oos'], 'sample': {
                        'value': {'bool': getattr(
                            self, 'fault_oos', False)}}},
                ],
                'parameters': [
                    {'name': LAL_NAME,
                     'values': {'max_shelve_ticks': {'int': BOUND}}}]}

    def http(self, method, url, body=None, timeout=10):
        if url.endswith('/signals'):
            return 200, self.signals
        if url.endswith('/schema'):
            return 200, schema_payload()
        if url.endswith('/command'):
            write = (body or {}).get('command', {}).get(
                'write_value', {})
            point, val = write.get('point'), write.get('value')
            if point == POINTS['lal_shelve']:
                self.shelve_req = bool(val.get('bool'))
            elif point == POINTS['ack']:
                if val == {'bool': True} and not self.ack_level:
                    self.ack_level = True
                    if self.latched:
                        self.latched = False
                        self._changed(POINTS['unack'], True, False)
                elif val == {'bool': False}:
                    self.ack_level = False
            elif point == POINTS['oos']:
                self.oos = bool(val.get('bool'))
            receipt = {'command': {'write_value': write},
                       'outcome': {'applied': {'tick': self.tick + 1}},
                       'actor': (body or {}).get('actor')}
            self._append({'command_settled': {'receipt': receipt}})
            return 200, receipt
        if url.endswith('/demote'):
            self._append({'role_changed': {
                'from': 'active', 'to': 'demoting'}})
            return 200, {'role': 'demoting'}
        if url.endswith('/promote'):
            peer = 'standby' if self.active == 'active' else 'active'
            self.active = peer
            self._append({'role_changed': {
                'from': 'standby', 'to': 'promoting'}})
            self._append({'role_changed': {
                'from': 'promoting', 'to': 'active'}})
            if self.drop_on_promote and self.latched:
                self.latched = False
                self._changed(POINTS['unack'], True, False)
            return 200, {'role': 'promoting'}
        if '/journal' in url:
            since = 0
            if 'since=' in url:
                since = int(url.split('since=')[1].split('&')[0])
            entries = [entry for entry in self.journal
                       if entry['seq'] > since]
            if self.journal_gap and len(entries) >= 3:
                entries = [entry for entry in entries
                           if entry['seq'] != since + 2]
            return 200, entries
        raise AssertionError('unexpected ' + method + ' ' + url)

    def plant_request(self, stream, request):
        if request.get('op') == 'ensure_writer':
            return {'result': 'claimed_shared'}
        if request.get('op') == 'write':
            if request.get('point') == POINTS['contact']:
                self.contact = bool(request['value'].get('bool'))
                return {'result': 'done'}
        raise AssertionError('unexpected plant ' + str(request))


def make_ctx(rig, tmp):
    return {'active': 'http://active', 'standby': 'http://standby',
            'plant': object(),
            'plant_owner': {'active': 1, 'standby': 2},
            'journal_files': {}, 'evidence_dir': str(tmp)}


def run_case(rig, tmp, **patches):
    ctx = make_ctx(rig, tmp)
    active = {'calls': 0}

    def settled(ctx):
        if rig.roles is not None:
            index = min(active['calls'], len(rig.roles) - 1)
            active['calls'] += 1
            return rig.roles[index]
        return rig.active

    base = {'http_json': rig.http,
            'save_evidence': scenarios.save_evidence,
            'wait_for': scenarios.wait_for,
            '_settled_active': settled,
            '_snapshot': lambda ctx, base: rig.snapshot(),
            '_try_snapshot': lambda ctx, base: rig.snapshot(),
            '_plant_connect': lambda ctx: object(),
            '_plant_request': rig.plant_request,
            '_field_inputs': lambda ctx: {POINTS['contact']},
            'CARRY_DEADLINE': 5}
    base.update(patches)
    patches_applied = [mock.patch.object(scenarios, key, value)
                       for key, value in base.items()]
    for patcher in patches_applied:
        patcher.start()
    try:
        return scenarios.scenario_managed_state_carryover(ctx)
    finally:
        for patcher in reversed(patches_applied):
            patcher.stop()


class CarryoverTests(unittest.TestCase):
    def test_registered_in_scenarios(self):
        self.assertIn(scenarios.scenario_managed_state_carryover,
                      scenarios.SCENARIOS)

    def test_clean_feed_passes_and_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = run_case(CarryRig(), Path(tmp))
        self.assertEqual(record['outcome'], 'passed',
                         record.get('detail'))
        qa_report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'a').mkdir()
            (Path(tmp) / 'b').mkdir()
            first = run_case(CarryRig(), Path(tmp) / 'a')
            second = run_case(CarryRig(), Path(tmp) / 'b')
        self.assertEqual(first['outcome'], 'passed')
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(second, sort_keys=True))

    def test_no_active_fails(self):
        rig = CarryRig()
        rig.roles = [None]
        with tempfile.TemporaryDirectory() as tmp:
            record = run_case(rig, Path(tmp))
        self.assertEqual(record['outcome'], 'failed')

    def test_missing_wiring_is_inconclusive(self):
        rig = CarryRig()
        rig.signals = signals_payload(omit=('lal-shelved',))
        with tempfile.TemporaryDirectory() as tmp:
            record = run_case(rig, Path(tmp))
        self.assertEqual(record['outcome'], 'inconclusive')

    def test_unwritable_shelve_is_inconclusive(self):
        rig = CarryRig()
        rig.signals = signals_payload(unwritable=('lal-shelve',))
        with tempfile.TemporaryDirectory() as tmp:
            record = run_case(rig, Path(tmp))
        self.assertEqual(record['outcome'], 'inconclusive')

    def test_switch_outside_bound_fails(self):
        rig = CarryRig()
        rig.tick_step = 5
        with tempfile.TemporaryDirectory() as tmp:
            record = run_case(rig, Path(tmp))
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('carryover-failed', record.get('detail', ''))

    def test_dropped_latch_fails(self):
        rig = CarryRig()
        rig.drop_on_promote = True
        with tempfile.TemporaryDirectory() as tmp:
            record = run_case(rig, Path(tmp))
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('carryover-failed', record.get('detail', ''))

    def test_gapped_journal_is_nondeterministic(self):
        rig = CarryRig()
        rig.journal_gap = True
        with tempfile.TemporaryDirectory() as tmp:
            record = run_case(rig, Path(tmp))
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('carryover-nondeterministic',
                      record.get('detail', ''))

    def test_roles_not_restored_fails(self):
        rig = CarryRig()
        rig.roles = ['active', 'standby', 'active', 'standby']
        with tempfile.TemporaryDirectory() as tmp:
            record = run_case(rig, Path(tmp))
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('roles did not restore',
                      record.get('detail', ''))


class CarryoverHelperTests(unittest.TestCase):
    def test_carry_points_map_names(self):
        points, missing = leg._carry_points(signals_payload())
        self.assertEqual(missing, [])
        self.assertEqual(points['lal_shelve'], POINTS['lal_shelve'])
        self.assertEqual(points['fault_oos'], POINTS['fault_oos'])

    def test_missing_signal_is_inconclusive(self):
        points, missing = leg._carry_points(
            signals_payload(omit=('p101-oos',)))
        self.assertIsNone(points)
        self.assertIn('p101-oos', ' '.join(missing))

    def test_demote_refused_fails(self):
        from qa_lane.scenarios import common
        case = common.Case('x', 'y', 'z')
        with tempfile.TemporaryDirectory() as tmp:
            ctx = {'active': 'http://active',
                   'standby': 'http://standby',
                   'evidence_dir': str(tmp)}
            with mock.patch.object(
                    scenarios, 'http_json',
                    return_value=(409, {'refused': True})), \
                    mock.patch.object(scenarios, '_settled_active',
                                      return_value='active'):
                peer, error = leg._switch_pair(ctx, case, 'carry')
        self.assertIsNone(peer)
        self.assertEqual(error['outcome'], 'failed')
        self.assertIn('demote refused', error.get('detail', ''))

    def test_promote_never_succeeding_fails(self):
        from qa_lane.scenarios import common
        case = common.Case('x', 'y', 'z')
        with tempfile.TemporaryDirectory() as tmp:
            ctx = {'active': 'http://active',
                   'standby': 'http://standby',
                   'evidence_dir': str(tmp)}
            calls = {'n': 0}

            def flaky(method, url, body=None, timeout=10):
                if url.endswith('/demote'):
                    return 200, {}
                calls['n'] += 1
                return 409, {'refused': True}

            with mock.patch.object(scenarios, 'http_json', flaky), \
                    mock.patch.object(scenarios, '_settled_active',
                                      return_value='active'):
                peer, error = leg._switch_pair(ctx, case, 'carry',
                                               deadline=0.1)
        self.assertIsNone(peer)
        self.assertEqual(error['outcome'], 'failed')
        self.assertIn('never succeeded', error.get('detail', ''))
        self.assertGreaterEqual(calls['n'], 1)


if __name__ == '__main__':
    unittest.main()
