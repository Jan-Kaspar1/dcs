"""The 3930_alarm_report leg's scenario unit coverage — the feed
fakes and TestCase classes for scenario_alarm_report. A fake monitor
serves the journal/snapshot/signals/history payloads, a host-side
journal fixture stands in for the durable file, and the
dcs-alarm-report subprocess seam is faked — covering the burst drive,
the report parsing and determinism assertion, the served-versus-file
comparison, and inconclusive handling. No shared file is edited.
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

leg = scenarios.scenario_alarm_report.__module__
leg = __import__(leg, fromlist=['x'])

EXPECTED_CASES = frozenset({
    'ReportTests.test_registered_in_scenarios',
    'ReportTests.test_clean_feed_passes_and_validates',
    'ReportTests.test_two_runs_produce_identical_evidence',
    'ReportTests.test_no_binary_path_is_inconclusive',
    'ReportTests.test_no_alarm_instances_is_inconclusive',
    'ReportTests.test_tool_exit_nonzero_fails',
    'ReportTests.test_missing_measure_fails',
    'ReportTests.test_count_mismatch_fails',
    'ReportTests.test_served_file_disagreement_fails',
    'ReportTests.test_nondeterministic_repeat_fails',
    'ReportTests.test_never_tripping_contact_fails',
    'ReportHelperTests.test_report_points_map_names',
    'ReportHelperTests.test_metric_misses_name_omitted_sections',
    'ReportHelperTests.test_metric_misses_name_count_mismatch',
    'ReportHelperTests.test_metric_misses_check_rate_totals',
    'ReportHelperTests.test_first_out_order_reads_first_true_ticks',
})

POINTS = {'contact': 1080, 'alarm': 1083, 'unack': 1084,
          'ack': 1081, 'level_primary': 10,
          'backup_alarm': 319, 'backup_unack': 419,
          'backup_ack': 519}


def signals_payload(omit=()):
    names = {'p101-moisture': 'contact',
             'p101-moisture-alarm': 'alarm',
             'p101-moisture-unacknowledged': 'unack',
             'p101-moisture-ack': 'ack',
             'level-primary': 'level_primary',
             'backup-active-alarm': 'backup_alarm',
             'backup-active-unacknowledged': 'backup_unack',
             'backup-active-ack': 'backup_ack'}
    entries = []
    for name, key in names.items():
        if name in omit:
            continue
        writable = key in ('ack', 'backup_ack')
        entries.append({'name': name, 'point': POINTS[key],
                        'direction': 'in' if key in (
                            'ack', 'backup_ack',
                            'contact', 'level_primary') else 'out',
                        'value_type': 'bool', 'writable': writable})
    return {'points': entries}


def make_report(moist, backup, drop_sections=(), drop_fields=()):
    """A minimal AlarmReport carrying the declared metric set — the
    shape the scenario audits."""
    alarms = [
        {'component': 'managed-bool-latching-alarm:30',
         'signal': 'p101-moisture-alarm',
         'alarm_point': POINTS['alarm'], 'priority': 2,
         'response_ticks': 60, 'activations': moist,
         'annunciations': moist, 'acknowledgments': moist},
        {'component': 'managed-bool-latching-alarm:9',
         'signal': 'backup-active-alarm',
         'alarm_point': POINTS['backup_alarm'], 'priority': 2,
         'response_ticks': 60, 'activations': backup,
         'annunciations': backup, 'acknowledgments': backup},
    ]
    for entry in alarms:
        for field in drop_fields:
            entry.pop(field, None)
    total = sum(entry.get('activations', 0) for entry in alarms)
    report = {'alarms': alarms,
              'rates': {'activations': total,
                        'annunciations': total},
              'responses': {'pairs': []},
              'standing': [],
              'priority_distribution': [],
              'source': {'journal_entries': total + 4,
                         'first_seq': 1, 'last_seq': total + 4}}
    for section in drop_sections:
        report.pop(section, None)
    return report


class Completed:
    """A captured subprocess result for the faked _run_report seam."""

    def __init__(self, stdout, returncode=0, stderr=''):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


class ReportRig:
    """A scripted monitor pair for the alarm-report scenario: the
    field-driven moisture alarm and the fault-driven backup alarm
    under the consumed edge, one continuous journal, and the
    host-side journal path the file run reads."""

    def __init__(self):
        self.tick = 0
        self.seq = 0
        self.contact = False
        self.level_fault = False
        self.moist = False
        self.moist_latch = False
        self.moist_ack = False
        self.backup = False
        self.backup_latch = False
        self.backup_ack = False
        self.journal = []
        self.signals = signals_payload()
        self.served = make_report(3, 1)
        self.filed = make_report(3, 1)
        self.served_repeat = None
        self.tool_calls = []
        self.tool_exit = 0

    def _append(self, event):
        self.seq += 1
        self.journal.append({'seq': self.seq, 'tick': self.tick,
                             'event': event})

    def _changed(self, point, was, now):
        self._append({'point_changed': {
            'point': point, 'from': {'bool': was},
            'to': {'bool': now}}})

    def snapshot(self):
        self.tick += 1
        if self.contact != self.moist:
            self._changed(POINTS['alarm'], self.moist, self.contact)
            self.moist = self.contact
            if self.moist and not self.moist_latch:
                self.moist_latch = True
                self._changed(POINTS['unack'], False, True)
        if self.level_fault != self.backup:
            self._changed(POINTS['backup_alarm'], self.backup,
                           self.level_fault)
            self.backup = self.level_fault
            if self.backup and not self.backup_latch:
                self.backup_latch = True
                self._changed(POINTS['backup_unack'], False, True)
        return {'tick': self.tick,
                'points': [
                    {'point': POINTS['contact'], 'sample': {
                        'value': {'bool': self.contact}}},
                    {'point': POINTS['alarm'], 'sample': {
                        'value': {'bool': self.moist}}},
                    {'point': POINTS['unack'], 'sample': {
                        'value': {'bool': self.moist_latch}}},
                    {'point': POINTS['ack'], 'sample': {
                        'value': {'bool': self.moist_ack}}},
                    {'point': POINTS['level_primary'], 'sample': {
                        'value': {'float': 1.0}}},
                    {'point': POINTS['backup_alarm'], 'sample': {
                        'value': {'bool': self.backup}}},
                    {'point': POINTS['backup_unack'], 'sample': {
                        'value': {'bool': self.backup_latch}}},
                    {'point': POINTS['backup_ack'], 'sample': {
                        'value': {'bool': self.backup_ack}}},
                ]}

    def http(self, method, url, body=None, timeout=10):
        if url.endswith('/signals'):
            return 200, self.signals
        if url.endswith('/command'):
            write = (body or {}).get('command', {}).get(
                'write_value', {})
            point, val = write.get('point'), write.get('value')
            if point == POINTS['ack']:
                if val == {'bool': True} and not self.moist_ack:
                    self.moist_ack = True
                    if self.moist_latch:
                        self.moist_latch = False
                        self._changed(POINTS['unack'], True, False)
                elif val == {'bool': False}:
                    self.moist_ack = False
            elif point == POINTS['backup_ack']:
                if val == {'bool': True} and not self.backup_ack:
                    self.backup_ack = True
                    if self.backup_latch:
                        self.backup_latch = False
                        self._changed(POINTS['backup_unack'], True,
                                       False)
                elif val == {'bool': False}:
                    self.backup_ack = False
            receipt = {'command': {'write_value': write},
                       'outcome': {'applied': {'tick': self.tick + 1}},
                       'actor': (body or {}).get('actor')}
            self._append({'command_settled': {'receipt': receipt}})
            return 200, receipt
        if '/journal' in url:
            since = 0
            if 'since=' in url:
                since = int(url.split('since=')[1].split('&')[0])
            return 200, [entry for entry in self.journal
                         if entry['seq'] > since]
        if '/history' in url:
            return 200, []
        raise AssertionError('unexpected ' + method + ' ' + url)

    def plant_request(self, stream, request):
        if request.get('op') == 'ensure_writer':
            return {'result': 'claimed_shared'}
        if request.get('op') == 'write':
            if request.get('point') == POINTS['contact']:
                self.contact = bool(request['value'].get('bool'))
                return {'result': 'done'}
        raise AssertionError('unexpected plant ' + str(request))

    def plant_ctl(self, ctx, *args):
        if args[0] == 'fault':
            self.level_fault = True
            return {'result': 'done'}
        if args[0] == 'clear-fault':
            self.level_fault = False
            return {'result': 'done'}
        raise AssertionError('unexpected plant-ctl ' + str(args))

    def run_report(self, binary, args):
        """The faked dcs-alarm-report seam: served versus file runs
        answer their canned documents; repeats answer the repeat
        canned text when the test stages one."""
        self.tool_calls.append(tuple(args))
        if self.tool_exit:
            return Completed('', returncode=self.tool_exit,
                             stderr='cannot read journal file')
        if '--journal-file' in args:
            return Completed(json.dumps(self.filed))
        if self.served_repeat is not None and len(
                [call for call in self.tool_calls
                 if '--journal-file' not in call]) > 1:
            return Completed(self.served_repeat)
        return Completed(json.dumps(self.served))


def make_ctx(rig, tmp, binary=True):
    ctx = {'active': 'http://active', 'standby': 'http://standby',
           'plant': object(),
           'plant_owner': {'active': 1, 'standby': 2},
           'journal_files': {'active': str(tmp / 'journal.jsonl')},
           'evidence_dir': str(tmp)}
    if binary:
        ctx['alarm_report'] = '/fake/dcs-alarm-report'
    return ctx


def run_case(rig, tmp, **patches):
    ctx = make_ctx(rig, tmp,
                   binary=patches.pop('binary', True))
    (tmp / 'journal.jsonl').write_text(
        json.dumps({'seq': 1, 'tick': 1, 'event': {}}) + '\n')
    base = {'http_json': rig.http,
            'save_evidence': scenarios.save_evidence,
            'wait_for': scenarios.wait_for,
            '_settled_active': lambda ctx: 'active',
            '_try_snapshot': lambda ctx, base: rig.snapshot(),
            '_plant_connect': lambda ctx: object(),
            '_plant_request': rig.plant_request,
            '_plant_ctl': rig.plant_ctl,
            '_try_plant_ctl': rig.plant_ctl,
            '_field_inputs': lambda ctx: {POINTS['contact']},
            'REPORT_DEADLINE': 5}
    base.update(patches)
    patches_applied = [mock.patch.object(scenarios, key, value)
                       for key, value in base.items()]
    for patcher in patches_applied:
        patcher.start()
    report_patch = mock.patch.object(scenarios, '_run_report',
                                     rig.run_report)
    report_patch.start()
    try:
        return scenarios.scenario_alarm_report(ctx)
    finally:
        report_patch.stop()
        for patcher in reversed(patches_applied):
            patcher.stop()


class ReportTests(unittest.TestCase):
    def test_registered_in_scenarios(self):
        self.assertIn(scenarios.scenario_alarm_report,
                      scenarios.SCENARIOS)

    def test_clean_feed_passes_and_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = run_case(ReportRig(), Path(tmp))
        self.assertEqual(record['outcome'], 'passed',
                         record.get('detail'))
        qa_report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'a').mkdir()
            (Path(tmp) / 'b').mkdir()
            first = run_case(ReportRig(), Path(tmp) / 'a')
            second = run_case(ReportRig(), Path(tmp) / 'b')
        self.assertEqual(first['outcome'], 'passed')
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(second, sort_keys=True))

    def test_no_binary_path_is_inconclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = run_case(ReportRig(), Path(tmp), binary=False)
        self.assertEqual(record['outcome'], 'inconclusive')

    def test_no_alarm_instances_is_inconclusive(self):
        rig = ReportRig()
        rig.signals = signals_payload(
            omit=('backup-active-alarm',
                  'backup-active-unacknowledged',
                  'backup-active-ack'))
        with tempfile.TemporaryDirectory() as tmp:
            record = run_case(rig, Path(tmp))
        self.assertEqual(record['outcome'], 'inconclusive')

    def test_tool_exit_nonzero_fails(self):
        rig = ReportRig()
        rig.tool_exit = 1
        with tempfile.TemporaryDirectory() as tmp:
            record = run_case(rig, Path(tmp))
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('alarm-report-failed', record.get('detail', ''))

    def test_missing_measure_fails(self):
        rig = ReportRig()
        rig.served = make_report(3, 1, drop_sections=('rates',))
        with tempfile.TemporaryDirectory() as tmp:
            record = run_case(rig, Path(tmp))
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('omits the declared section',
                      record.get('detail', ''))

    def test_count_mismatch_fails(self):
        rig = ReportRig()
        rig.served = make_report(0, 1)
        rig.filed = make_report(0, 1)
        with tempfile.TemporaryDirectory() as tmp:
            record = run_case(rig, Path(tmp))
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('activations', record.get('detail', ''))

    def test_served_file_disagreement_fails(self):
        rig = ReportRig()
        rig.filed = make_report(3, 1)
        rig.filed['source'] = {'journal_entries': 4, 'first_seq': 1,
                               'last_seq': 3}
        with tempfile.TemporaryDirectory() as tmp:
            record = run_case(rig, Path(tmp))
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('overlapping window', record.get('detail', ''))

    def test_nondeterministic_repeat_fails(self):
        rig = ReportRig()
        rig.served_repeat = json.dumps(make_report(3, 0))
        with tempfile.TemporaryDirectory() as tmp:
            record = run_case(rig, Path(tmp))
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('differ', record.get('detail', ''))

    def test_never_tripping_contact_fails(self):
        rig = ReportRig()
        rig.plant_request = lambda stream, request: \
            {'result': 'done'}
        with tempfile.TemporaryDirectory() as tmp:
            record = run_case(rig, Path(tmp))
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('alarm-report-failed', record.get('detail', ''))


class ReportHelperTests(unittest.TestCase):
    def test_report_points_map_names(self):
        points, missing = leg._report_points(signals_payload())
        self.assertEqual(missing, [])
        self.assertEqual(points['contact'], POINTS['contact'])
        self.assertEqual(points['backup_alarm'],
                         POINTS['backup_alarm'])

    def test_metric_misses_name_omitted_sections(self):
        misses = leg._metric_misses(
            make_report(3, 1, drop_sections=('rates', 'standing')),
            {POINTS['alarm']: 3, POINTS['backup_alarm']: 1})
        self.assertTrue(any('rates' in miss for miss in misses))
        self.assertTrue(any('standing' in miss for miss in misses))

    def test_metric_misses_name_count_mismatch(self):
        misses = leg._metric_misses(
            make_report(3, 1),
            {POINTS['alarm']: 9, POINTS['backup_alarm']: 1})
        self.assertTrue(any('9' in miss for miss in misses))

    def test_metric_misses_check_rate_totals(self):
        report = make_report(3, 1)
        report['rates'] = {'activations': 0, 'annunciations': 0}
        misses = leg._metric_misses(
            report, {POINTS['alarm']: 3, POINTS['backup_alarm']: 1})
        self.assertTrue(any('rates section' in miss
                            for miss in misses))

    def test_first_out_order_reads_first_true_ticks(self):
        journal = [
            {'seq': 1, 'tick': 4, 'event': {'point_changed': {
                'point': 7, 'to': {'bool': True}}}},
            {'seq': 2, 'tick': 6, 'event': {'point_changed': {
                'point': 9, 'to': {'bool': True}}}},
        ]
        self.assertEqual(
            leg._first_out_order(journal, [7, 9]), [7, 9])
        self.assertEqual(
            leg._first_out_order(journal, [9, 7]), [7, 9])


if __name__ == '__main__':
    unittest.main()
