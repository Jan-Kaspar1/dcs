"""The 3910_first_out_burst leg's scenario unit coverage — the feed
fakes and TestCase classes for scenario_first_out_burst. The shared
fakes and helpers live beside each leg's own test module; no shared
file is edited. EXPECTED_CASES pins this module's contribution to the
suite's case coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from qa_lane import report as qa_report
from qa_lane import scenarios

leg = scenarios.scenario_first_out_burst.__module__
leg = __import__(leg, fromlist=['x'])

EXPECTED_CASES = frozenset({
    'BurstTests.test_registered_in_scenarios',
    'BurstTests.test_clean_feed_passes_and_validates',
    'BurstTests.test_two_runs_produce_identical_evidence',
    'BurstTests.test_no_active_fails',
    'BurstTests.test_missing_wiring_is_inconclusive',
    'BurstTests.test_unwritable_ack_is_inconclusive',
    'BurstTests.test_unreported_alarm_is_inconclusive',
    'BurstTests.test_never_asserting_alarm_fails',
    'BurstTests.test_reordered_journal_fails',
    'BurstTests.test_history_disagreement_is_nondeterministic',
    'BurstTests.test_role_move_fails',
    'BurstHelperTests.test_burst_points_map_names',
    'BurstHelperTests.test_missing_signal_is_inconclusive',
    'BurstHelperTests.test_activation_order_reads_first_true_ticks',
    'BurstHelperTests.test_ordered_misses_name_dropped_transitions',
    'BurstHelperTests.test_ordered_misses_name_reordered_transitions',
})

POINTS = {
    'level_primary': 10, 'backup_active': 219,
    'backup_alarm': 319, 'backup_unack': 419, 'backup_ack': 519,
    'power_fail': 120, 'power_alarm': 320, 'power_unack': 420,
    'power_ack': 520, 'none_alarm': 321, 'none_unack': 421,
    'none_ack': 521, 'faulted_alarm': 322, 'faulted_unack': 422,
    'faulted_ack': 522, 'run1': 40, 'run2': 41,
    'fault_alarm1': 323, 'fault_unack1': 423,
    'fault_alarm2': 324, 'fault_unack2': 424,
}
ALARMS = (('backup_alarm', 'backup_unack', 'backup_ack',
           lambda rig: rig.level_fault),
          ('power_alarm', 'power_unack', 'power_ack',
           lambda rig: rig.power_fail),
          ('none_alarm', 'none_unack', 'none_ack',
           lambda rig: rig.power_fail),
          ('faulted_alarm', 'faulted_unack', 'faulted_ack',
           lambda rig: rig.run1_fault and rig.run2_fault))


def signals_payload(omit=(), unwritable=()):
    """The /signals payload naming the cascade — `omit` drops names,
    `unwritable` serves the ack entries without the writable mark."""
    names = {'level-primary': 'level_primary',
             'backup-active': 'backup_active',
             'backup-active-alarm': 'backup_alarm',
             'backup-active-unacknowledged': 'backup_unack',
             'backup-active-ack': 'backup_ack',
             'power-fail': 'power_fail',
             'power-fail-alarm': 'power_alarm',
             'power-fail-unacknowledged': 'power_unack',
             'power-fail-ack': 'power_ack',
             'none-available-alarm': 'none_alarm',
             'none-available-unacknowledged': 'none_unack',
             'none-available-ack': 'none_ack',
             'all-faulted-alarm': 'faulted_alarm',
             'all-faulted-unacknowledged': 'faulted_unack',
             'all-faulted-ack': 'faulted_ack',
             'p101-run': 'run1', 'p102-run': 'run2',
             'p101-fault-alarm': 'fault_alarm1',
             'p101-fault-unacknowledged': 'fault_unack1',
             'p102-fault-alarm': 'fault_alarm2',
             'p102-fault-unacknowledged': 'fault_unack2'}
    entries = []
    for name, key in names.items():
        if name in omit:
            continue
        point = POINTS[key]
        writable = key.endswith('_ack') and name not in unwritable
        entries.append({'name': name, 'point': point,
                        'direction': 'in' if key.endswith('_ack')
                        or key in ('level_primary', 'power_fail',
                                   'run1', 'run2') else 'out',
                        'value_type': 'bool'
                        if key not in ('level_primary',) else 'float'
                        if key == 'level_primary' else 'bool',
                        'writable': writable})
    return {'points': entries}


class BurstRig:
    """A scripted monitor pair for the burst scenario: one scan per
    _try_snapshot call — the quality/field drives feed the four
    managed latches under the consumed edge, ack writes clear them,
    and every transition journals. Fault flags stage each named
    failure the issue calls out."""

    def __init__(self):
        self.tick = 0
        self.seq = 0
        self.level_fault = False
        self.power_fail = False
        self.run1_fault = False
        self.run2_fault = False
        self.alarm = {}
        self.latch = {}
        self.ack_level = {}
        self.journal = []
        self.active = 'active'
        self.reorder = False
        self.history_skew = False
        self.signals = signals_payload()

    def _append(self, event):
        self.seq += 1
        self.journal.append({'seq': self.seq, 'tick': self.tick,
                             'event': event})

    def snapshot(self):
        """One completed scan: evaluate the cascade, journal the
        transitions, and serve the snapshot."""
        self.tick += 1
        for alarm, unack, ack, cond in ALARMS:
            want = bool(cond(self))
            was = self.alarm.get(alarm, False)
            if want != was:
                self.alarm[alarm] = want
                self._append({'point_changed': {
                    'point': POINTS[alarm],
                    'from': {'bool': was}, 'to': {'bool': want}}})
            if want and not was:
                self.latch[alarm] = True
                self._append({'point_changed': {
                    'point': POINTS[unack],
                    'from': {'bool': False},
                    'to': {'bool': True}}})
        samples = []
        for key, point in POINTS.items():
            if key == 'level_primary':
                samples.append({'point': point, 'sample': {
                    'value': {'float': 1.0}}})
                continue
            if key in ('backup_active',):
                samples.append({'point': point, 'sample': {
                    'value': {'bool': self.level_fault}}})
                continue
            if key == 'power_fail':
                samples.append({'point': point, 'sample': {
                    'value': {'bool': self.power_fail}}})
                continue
            if key in ('run1', 'run2'):
                samples.append({'point': point, 'sample': {
                    'value': {'bool': True}}})
                continue
            if key.endswith('_alarm'):
                base = key
                samples.append({'point': point, 'sample': {
                    'value': {'bool': self.alarm.get(base, False)}}})
            elif key.endswith('_unack'):
                base = key[:-len('_unack')] + '_alarm'
                samples.append({'point': point, 'sample': {
                    'value': {'bool': self.latch.get(base, False)}}})
            elif key.endswith('_ack'):
                samples.append({'point': point, 'sample': {
                    'value': {'bool': self.ack_level.get(key, False)}}})
            elif key.startswith('fault_alarm'):
                mirror = 'faulted_alarm'
                samples.append({'point': point, 'sample': {
                    'value': {'bool': self.alarm.get(mirror, False)}}})
            elif key.startswith('fault_unack'):
                samples.append({'point': point, 'sample': {
                    'value': {'bool': self.latch.get(
                        'faulted_alarm', False)}}})
            else:
                samples.append({'point': point, 'sample': {
                    'value': {'bool': False}}})
        return {'tick': self.tick, 'points': samples}

    def http(self, method, url, body=None, timeout=10):
        """The http_json seam: signals, command, journal, history."""
        if url.endswith('/signals'):
            return 200, self.signals
        if url.endswith('/command'):
            write = (body or {}).get('command', {}).get(
                'write_value', {})
            point, val = write.get('point'), write.get('value')
            receipt = {'command': {'write_value': write},
                       'outcome': {'applied': {'tick': self.tick + 1}},
                       'actor': (body or {}).get('actor')}
            for alarm, unack, ack, _cond in ALARMS:
                if POINTS[ack] == point:
                    if val == {'bool': True} \
                            and not self.ack_level.get(ack, False):
                        self.ack_level[ack] = True
                        if self.latch.get(alarm, False):
                            self.latch[alarm] = False
                            self._append({'point_changed': {
                                'point': POINTS[unack],
                                'from': {'bool': True},
                                'to': {'bool': False}}})
                    elif val == {'bool': False}:
                        self.ack_level[ack] = False
            self._append({'command_settled': {'receipt': receipt}})
            return 200, receipt
        if '/journal' in url:
            since = 0
            if 'since=' in url:
                since = int(url.split('since=')[1].split('&')[0])
            entries = [entry for entry in self.journal
                       if entry['seq'] > since]
            if self.reorder:
                entries = [dict(entry) for entry in entries]

                def activation(point):
                    return next(
                        (entry for entry in entries
                         if entry['event'].get('point_changed', {})
                         .get('point') == point
                         and entry['event']['point_changed'].get(
                             'to') == {'bool': True}),
                        None)

                first = activation(POINTS['backup_alarm'])
                second = activation(POINTS['power_alarm'])
                if first is not None and second is not None:
                    # A reordered durable record: the power
                    # activation journaled ahead of the backup one.
                    first['tick'], second['tick'] = second['tick'], \
                        first['tick']
            return 200, entries
        if '/history' in url:
            points = set()
            for part in url.replace('?', '&').split('&'):
                if part.startswith('point='):
                    points.add(int(part.split('=')[1]))
            entries = []
            for point in sorted(points):
                samples = [{'sample': {'value': {'bool': False},
                                       'tick': 0}}]
                for entry in self.journal:
                    change = entry['event'].get('point_changed',
                                                {})
                    if change.get('point') != point:
                        continue
                    tick = entry['tick']
                    if self.history_skew and point == POINTS[
                            'backup_alarm'] \
                            and change.get('to') == {'bool': True}:
                        tick = 9999
                    samples.append({'sample': {
                        'value': change.get('to'), 'tick': tick}})
                entries.append({'point': point, 'samples': samples})
            return 200, entries
        raise AssertionError('unexpected ' + method + ' ' + url)

    def plant_ctl(self, ctx, *args):
        """The dcs-plant-ctl seam: fault/clear-fault by point."""
        if args[0] == 'fault':
            point = int(args[1])
            if point == POINTS['level_primary']:
                self.level_fault = True
            elif point == POINTS['run1']:
                self.run1_fault = True
            elif point == POINTS['run2']:
                self.run2_fault = True
            return {'result': 'done'}
        if args[0] == 'clear-fault':
            point = int(args[1])
            if point == POINTS['level_primary']:
                self.level_fault = False
            elif point == POINTS['run1']:
                self.run1_fault = False
            elif point == POINTS['run2']:
                self.run2_fault = False
            return {'result': 'done'}
        raise AssertionError('unexpected plant-ctl ' + str(args))

    def plant_request(self, stream, request):
        """The raw plant-protocol seam: shared claim and writes."""
        if request.get('op') == 'ensure_writer':
            return {'result': 'claimed_shared'}
        if request.get('op') == 'write':
            if request.get('point') == POINTS['power_fail']:
                self.power_fail = bool(
                    request['value'].get('bool'))
                return {'result': 'done'}
        raise AssertionError('unexpected plant ' + str(request))


def make_ctx(rig, tmp):
    return {'active': 'http://active', 'standby': 'http://standby',
            'plant': object(),
            'plant_owner': {'active': 1, 'standby': 2},
            'journal_files': {}, 'evidence_dir': str(tmp)}


def run_case(rig, tmp, **patches):
    """Run the scenario against the rig with the common seams
    patched — the simulated-rig runner path in miniature."""
    ctx = make_ctx(rig, tmp)
    base = {'http_json': rig.http,
            'save_evidence': scenarios.save_evidence,
            'wait_for': scenarios.wait_for,
            '_settled_active': lambda ctx: rig.active,
            '_try_snapshot': lambda ctx, base: rig.snapshot(),
            '_plant_connect': lambda ctx: object(),
            '_plant_request': rig.plant_request,
            '_plant_ctl': rig.plant_ctl,
            '_try_plant_ctl': rig.plant_ctl,
            '_field_inputs': lambda ctx: {POINTS['level_primary'],
                                          POINTS['power_fail']},
            'BURST_DEADLINE': 5}
    base.update(patches)
    patches_applied = [mock.patch.object(scenarios, key, value)
                       for key, value in base.items()]
    for patcher in patches_applied:
        patcher.start()
    try:
        return scenarios.scenario_first_out_burst(ctx)
    finally:
        for patcher in reversed(patches_applied):
            patcher.stop()


class BurstTests(unittest.TestCase):
    def test_registered_in_scenarios(self):
        self.assertIn(scenarios.scenario_first_out_burst,
                      scenarios.SCENARIOS)

    def test_clean_feed_passes_and_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = run_case(BurstRig(), Path(tmp))
        self.assertEqual(record['outcome'], 'passed')
        qa_report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'a').mkdir()
            (Path(tmp) / 'b').mkdir()
            first = run_case(BurstRig(), Path(tmp) / 'a')
            second = run_case(BurstRig(), Path(tmp) / 'b')
        self.assertEqual(first['outcome'], 'passed')
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(second, sort_keys=True))

    def test_no_active_fails(self):
        rig = BurstRig()
        rig.active = None
        with tempfile.TemporaryDirectory() as tmp:
            record = run_case(rig, Path(tmp))
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('no peer reports role=active',
                      record.get('detail', ''))

    def test_missing_wiring_is_inconclusive(self):
        rig = BurstRig()
        rig.signals = signals_payload(
            omit=('power-fail-alarm',))
        with tempfile.TemporaryDirectory() as tmp:
            record = run_case(rig, Path(tmp))
        self.assertEqual(record['outcome'], 'inconclusive')

    def test_unwritable_ack_is_inconclusive(self):
        rig = BurstRig()
        rig.signals = signals_payload(
            unwritable=('power-fail-ack',))
        with tempfile.TemporaryDirectory() as tmp:
            record = run_case(rig, Path(tmp))
        self.assertEqual(record['outcome'], 'inconclusive')

    def test_unreported_alarm_is_inconclusive(self):
        rig = BurstRig()
        real_snapshot = rig.snapshot

        def dropping():
            snap = real_snapshot()
            snap = dict(snap)
            snap['points'] = [entry for entry in snap['points']
                              if entry['point']
                              != POINTS['backup_alarm']]
            return snap

        with tempfile.TemporaryDirectory() as tmp:
            record = run_case(rig, Path(tmp),
                              _try_snapshot=lambda ctx,
                              base: dropping())
        self.assertEqual(record['outcome'], 'inconclusive')

    def test_never_asserting_alarm_fails(self):
        rig = BurstRig()
        rig.plant_ctl = lambda ctx, *args: {'result': 'done'}
        with tempfile.TemporaryDirectory() as tmp:
            record = run_case(rig, Path(tmp))
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('burst-order-failed', record.get('detail', ''))

    def test_reordered_journal_fails(self):
        rig = BurstRig()
        rig.reorder = True
        with tempfile.TemporaryDirectory() as tmp:
            record = run_case(rig, Path(tmp))
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('missing or out of order',
                      record.get('detail', ''))

    def test_history_disagreement_is_nondeterministic(self):
        rig = BurstRig()
        rig.history_skew = True
        with tempfile.TemporaryDirectory() as tmp:
            record = run_case(rig, Path(tmp))
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('burst-order-nondeterministic',
                      record.get('detail', ''))

    def test_role_move_fails(self):
        rig = BurstRig()
        roles = iter(['active', 'standby'])
        with tempfile.TemporaryDirectory() as tmp:
            record = run_case(
                rig, Path(tmp),
                _settled_active=lambda ctx: next(roles, 'standby'))
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('active role moved', record.get('detail', ''))


class BurstHelperTests(unittest.TestCase):
    def test_burst_points_map_names(self):
        points, missing = leg._burst_points(signals_payload())
        self.assertEqual(missing, [])
        self.assertEqual(points['power_fail'], POINTS['power_fail'])
        self.assertEqual(points['faulted_alarm'],
                         POINTS['faulted_alarm'])

    def test_missing_signal_is_inconclusive(self):
        points, missing = leg._burst_points(
            signals_payload(omit=('power-fail',)))
        self.assertIsNone(points)
        self.assertIn('power-fail', ' '.join(missing))

    def test_activation_order_reads_first_true_ticks(self):
        journal = [
            {'seq': 1, 'tick': 4, 'event': {'point_changed': {
                'point': 1, 'to': {'bool': True}}}},
            {'seq': 2, 'tick': 6, 'event': {'point_changed': {
                'point': 2, 'to': {'bool': True}}}},
            {'seq': 3, 'tick': 7, 'event': {'point_changed': {
                'point': 1, 'to': {'bool': False}}}},
        ]
        self.assertEqual(
            leg._activation_order(journal, [1, 2, 3]), [1, 2])

    def test_ordered_misses_name_dropped_transitions(self):
        (miss,) = leg._ordered_misses([10], [[10], [20]])
        self.assertIn('20', miss)
        self.assertIn('group 1', miss)

    def test_ordered_misses_name_reordered_transitions(self):
        misses = leg._ordered_misses([20, 10], [[10], [20]])
        self.assertTrue(misses)
        self.assertIn('group 1', misses[0])


if __name__ == '__main__':
    unittest.main()
