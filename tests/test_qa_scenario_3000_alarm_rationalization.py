"""The 3000_alarm_rationalization leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_alarm_rationalization, split out of the
test_qa_scenarios monolith (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'AlarmRationalizationTests.test_registered',
    'AlarmRationalizationTests.test_clean_feed_passes_and_validates',
    'AlarmRationalizationTests.test_two_runs_produce_identical_evidence',
    'AlarmRationalizationTests.test_missing_rationalization_block_fails',
    'AlarmRationalizationTests.test_record_set_disagreement_fails',
    'AlarmRationalizationTests.test_wrong_declared_values_fail',
    'AlarmRationalizationTests.test_unserved_parameter_entry_fails',
    'AlarmRationalizationTests.test_unjournaled_standing_point_fails',
    'AlarmRationalizationTests.test_misbound_alarm_point_fails',
    'AlarmRationalizationTests.test_refused_retune_fails',
    'AlarmRationalizationTests.test_unserved_retune_fails',
    'AlarmRationalizationTests.test_refused_restore_fails',
    'AlarmRationalizationTests.test_drifted_reread_fails_nondeterministic',
    'AlarmRationalizationTests.test_omitted_components_section_is_inconclusive',
    'AlarmRationalizationTests.test_omitted_parameters_section_is_inconclusive',
    'AlarmRationalizationTests.test_no_managed_records_is_inconclusive',
    'AlarmRationalizationTests.test_missing_pinned_instance_is_inconclusive',
    'AlarmRationalizationTests.test_missing_journal_path_is_inconclusive',
    'AlarmRationalizationTests.test_no_active_peer_fails',
})


class RationalizationFeed:
    """A stubbed field owner for the alarm-rationalization scenario.
    ctrl-a is the settled active serving the rig's managed-alarm
    record: /signals carries each instance's ComponentRecord — the
    rationalization block beside the kind:id name — plus the standing
    alarm points' Signal entries; /snapshot carries the bound-point
    descriptors and the live parameter report; POST /command receipts
    a set_parameter retune that applies at the next request; and the
    run's journal file carries every standing point's first-observed
    point_changed record — the durable record's real format, which the
    feed writes itself. Fault flags stage each named miss the issue
    calls out."""

    # name -> (kind, declared (priority, class, response_ticks),
    #          reference signal, alarm point) — the rig model's real
    #   triples and identity joins for the pinned instances.
    ALARMS = {
        'managed-latching-alarm:5': (
            'managed-latching-alarm', (1, 1, 30), 'lah-alarm', 1003),
        'managed-bool-latching-alarm:11': (
            'managed-bool-latching-alarm', (1, 1, 30),
            'power-fail-alarm', 1053),
        'managed-bool-latching-alarm:22': (
            'managed-bool-latching-alarm', (2, 2, 60),
            'p101-fault-alarm', 1073),
        'managed-bool-latching-alarm:36': (
            'managed-bool-latching-alarm', (2, 2, 60),
            'p102-fault-alarm', 1103)}

    def __init__(self, journal_path):
        self.journal_path = Path(journal_path)
        self.journal_written = False
        self.tick = 0
        self.receipts = []
        self.posts = 0
        self.signals_reads = 0
        # name -> live {'priority', 'class', 'response_ticks'}
        self.values = {name: {'priority': spec[1][0],
                              'class': spec[1][1],
                              'response_ticks': spec[1][2]}
                       for name, spec in self.ALARMS.items()}
        # Fault injection for the named-failure cases.
        self.not_active = False       # no peer reports role=active
        self.omit_components = False  # /signals drops the section
        self.omit_parameters = False  # /snapshot drops the section
        self.no_records = False       # no managed alarm either side
        self.drop_record = False      # one instance unrecorded
        self.drop_block = False       # one record loses its prose
        self.drop_pin = False         # a pinned instance unserved
        self.drop_param = False       # one instance unserved live
        self.wrong_values = False     # the wet-well triple drifts
        self.misbound = False         # an alarm port binds elsewhere
        self.silent_journal = False   # a standing point unjournaled
        self.refuse_tune = False      # every retune settles rejected
        self.refuse_restore = False   # the restore settles rejected
        self.never_serve = False      # the applied tune never serves
        self.drift_reread = False     # the re-read disagrees

    def _write_journal(self):
        self.journal_written = True
        records = [{'run_boundary': {'run': 1, 'tick': 0}}]
        seq = 1
        for name in sorted(self.ALARMS):
            point = self.ALARMS[name][3]
            if self.silent_journal and point == 1073:
                continue
            records.append({'entry': {'seq': seq, 'tick': 1,
                                      'event': {'point_changed': {
                                          'point': point,
                                          'from': None,
                                          'to': {'bool': False}}}}})
            seq += 1
        self.journal_path.write_text(
            ''.join(json.dumps(record) + '\n' for record in records))

    def _apply(self):
        # The scan boundary's command phase: an accepted tune settles
        # applied and the parameter report picks it up — unless the
        # fault flag holds the report stale.
        for receipt in self.receipts:
            accepted = (receipt.get('outcome') or {}).get('accepted')
            if accepted and self.tick >= accepted['apply_tick']:
                tune = receipt['command']['set_parameter']
                receipt['outcome'] = {'applied': {'tick': self.tick}}
                if not self.never_serve:
                    self.values[tune['component']][tune['name']] = \
                        tune['value']['int']

    def _signals(self):
        self.signals_reads += 1
        records = []
        for name, spec in self.ALARMS.items():
            if self.no_records or self.drop_pin \
                    and name == 'managed-bool-latching-alarm:11' \
                    or self.drop_record \
                    and name == 'managed-bool-latching-alarm:36':
                continue
            block = None if self.drop_block \
                and name == 'managed-bool-latching-alarm:22' else {
                    'consequence': 'the ' + spec[2] + ' condition '
                                   'stands unanswered',
                    'required_action': 'answer it',
                    'reference': spec[2]}
            if self.drift_reread and self.signals_reads > 1 \
                    and name == 'managed-latching-alarm:5' \
                    and block is not None:
                block = dict(block, consequence='a moved answer')
            record = {'name': name, 'kind': spec[0]}
            if block is not None:
                record['rationalization'] = block
            records.append(record)
        points = [{'point': spec[3], 'signal': 11000 + spec[3],
                   'name': spec[2], 'direction': 'out',
                   'value_type': 'bool', 'writable': False}
                  for name, spec in self.ALARMS.items()
                  if not self.no_records]
        body = {'points': points}
        if not self.omit_components:
            body['components'] = records
        return 200, body

    def _snapshot(self):
        descriptors = [
            {'name': name, 'kind': spec[0], 'label': name,
             'ports': [{'name': 'alarm', 'direction': 'out',
                        'kind': 'bool', 'role': 'status',
                        'point': 9999 if self.misbound
                        and name == 'managed-bool-latching-alarm:22'
                        else spec[3]}],
             'parameters': [{'name': 'priority', 'kind': 'int'},
                            {'name': 'class', 'kind': 'int'},
                            {'name': 'response_ticks', 'kind': 'int'}],
             'commands': [], 'events': []}
            for name, spec in self.ALARMS.items()
            if not self.no_records and not (
                self.drop_pin
                and name == 'managed-bool-latching-alarm:11')]
        parameters = []
        for name in self.ALARMS:
            if self.no_records or self.drop_pin \
                    and name == 'managed-bool-latching-alarm:11' \
                    or self.drop_param \
                    and name == 'managed-bool-latching-alarm:36':
                continue
            values = dict(self.values[name])
            if self.wrong_values \
                    and name == 'managed-latching-alarm:5':
                values['response_ticks'] = 99
            parameters.append(
                {'name': name,
                 'values': {param: {'int': value}
                            for param, value in values.items()}})
        body = {'tick': self.tick, 'descriptors': descriptors}
        if not self.omit_parameters:
            body['parameters'] = parameters
        return 200, body

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, _query = path.partition('?')
        self.tick += 1
        if not self.journal_written:
            self._write_journal()
        self._apply()
        if host != 'ctrl-a:1':
            raise AssertionError('unexpected request %s %s'
                                 % (method, url))
        if (method, route) == ('GET', '/role'):
            role = 'standby' if self.not_active else 'active'
            return 200, {'role': role, 'tick': self.tick}
        if (method, route) == ('GET', '/signals'):
            return self._signals()
        if (method, route) == ('GET', '/snapshot'):
            return self._snapshot()
        if (method, route) == ('GET', '/receipts'):
            return 200, list(self.receipts)
        if (method, route) == ('POST', '/command'):
            self.posts += 1
            command = body['command']
            if self.refuse_tune \
                    or self.refuse_restore and self.posts > 1:
                receipt = {'command': command,
                           'outcome': {'rejected': {'reason': {
                               'validation': {'detail': 'refused'}}}},
                           'actor': body.get('actor')}
                self.receipts.append(receipt)
                return 200, receipt
            receipt = {'command': command,
                       'outcome': {'accepted': {
                           'apply_tick': self.tick + 1}},
                       'actor': body.get('actor')}
            self.receipts.append(receipt)
            return 200, receipt
        raise AssertionError('unexpected request %s %s' % (method, url))


class AlarmRationalizationTests(unittest.TestCase):
    """scenario_alarm_rationalization against the stubbed field owner:
    the components-section coverage, the live parameter report, the
    durable journal's identity join, the receipted retune-and-restore,
    and the inconclusive cases when the served sections omit the
    records."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.journal = Path(self.tmp.name) / 'controllers' / 'a' \
            / 'journal.jsonl'
        self.journal.parent.mkdir(parents=True)
        self.feed = RationalizationFeed(self.journal)

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self, feed=None, journal=True):
        feed = feed or self.feed
        ctx = {'active': 'http://ctrl-a:1',
               'evidence_dir': str(self.evidence)}
        if journal:
            ctx['journal_files'] = {'active': str(self.journal)}
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'RATIONALIZATION_SETTLE', 0.3), \
                patch.object(scenarios,
                             'RATIONALIZATION_DEADLINE', 0.5):
            return scenarios.scenario_alarm_rationalization(ctx)

    def test_registered(self):
        self.assertIn(scenarios.scenario_alarm_rationalization,
                      scenarios.SCENARIOS)
        self.assertIs(
            verify.case_function('alarm-rationalization'),
            scenarios.scenario_alarm_rationalization)

    def test_clean_feed_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)

    def test_two_runs_produce_identical_evidence(self):
        # The deterministic-rerun contract: two runs against the same
        # rig layout record the same report and the same evidence
        # files — the feed's transitions are request-count keyed,
        # never wall-clock.
        runs = []
        for _index in range(2):
            for stale in self.evidence.iterdir():
                stale.unlink()
            feed = RationalizationFeed(self.journal)
            record = self.run_scenario(feed=feed)
            runs.append((record, {p.name: p.read_bytes()
                                  for p in self.evidence.iterdir()}))
        self.assertEqual(runs[0][0]['outcome'], 'passed', runs[0][0])
        self.assertEqual(runs[0], runs[1])

    def test_missing_rationalization_block_fails(self):
        # One record's prose block never served — the record's
        # declared-once half missing on the deployed surface.
        self.feed.drop_block = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('rationalization-failed',
                      record.get('detail', ''))
        self.assertIn('rationalization block',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_record_set_disagreement_fails(self):
        # The descriptor set carries an instance the components
        # section never records — one record per managed alarm
        # instance is the contract.
        self.feed.drop_record = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('rationalization-failed',
                      record.get('detail', ''))
        self.assertIn('disagree', record.get('detail', ''))
        report.validate_scenario(record)

    def test_wrong_declared_values_fail(self):
        # The wet-well alarm's served triple drifts off the declared
        # 1/1/30 — a live-parameter miss, not a prose one.
        self.feed.wrong_values = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('rationalization-failed',
                      record.get('detail', ''))
        self.assertIn('managed-latching-alarm:5',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unserved_parameter_entry_fails(self):
        # One managed instance's live parameter report never lands.
        self.feed.drop_param = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('rationalization-failed',
                      record.get('detail', ''))
        self.assertIn('parameters entry', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unjournaled_standing_point_fails(self):
        # The durable record never names p101-fault-alarm's point —
        # the identity join's journal half missing.
        self.feed.silent_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('rationalization-failed',
                      record.get('detail', ''))
        self.assertIn('point_changed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_misbound_alarm_point_fails(self):
        # The reference signal and the bound alarm port disagree —
        # the identity convention broken.
        self.feed.misbound = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('rationalization-failed',
                      record.get('detail', ''))
        self.assertIn('alarm port binds', record.get('detail', ''))
        report.validate_scenario(record)

    def test_refused_retune_fails(self):
        self.feed.refuse_tune = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('rationalization-failed',
                      record.get('detail', ''))
        self.assertIn('never settled applied',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unserved_retune_fails(self):
        # The receipt settles applied but the parameter report never
        # serves the tuned value — the served half of the tune broke.
        self.feed.never_serve = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('rationalization-failed',
                      record.get('detail', ''))
        self.assertIn('never served', record.get('detail', ''))
        report.validate_scenario(record)

    def test_refused_restore_fails(self):
        # The retune applies and serves but the restore is refused —
        # the leg leaves the tuned value standing.
        self.feed.refuse_restore = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('rationalization-failed',
                      record.get('detail', ''))
        self.assertIn('restore', record.get('detail', ''))
        report.validate_scenario(record)

    def test_drifted_reread_fails_nondeterministic(self):
        # The re-read serves a different rationalization block — the
        # same surface disagreeing with itself is the named
        # nondeterministic miss.
        self.feed.drift_reread = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('rationalization-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_omitted_components_section_is_inconclusive(self):
        self.feed.omit_components = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_omitted_parameters_section_is_inconclusive(self):
        self.feed.omit_parameters = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_no_managed_records_is_inconclusive(self):
        # Neither section carries a managed alarm — the deployed
        # model is not the rig's rationalized set.
        self.feed.no_records = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_missing_pinned_instance_is_inconclusive(self):
        # The served set agrees with itself but never carries a
        # pinned instance — the deployed model is not the rig's.
        self.feed.drop_pin = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('pinned', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_journal_path_is_inconclusive(self):
        record = self.run_scenario(journal=False)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('journal-file path', record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_active_peer_fails(self):
        self.feed.not_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no peer reports role=active',
                      record.get('detail', ''))
        report.validate_scenario(record)


if __name__ == '__main__':
    unittest.main()
