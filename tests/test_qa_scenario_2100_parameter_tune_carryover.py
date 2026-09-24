"""The 2100_parameter_tune_carryover leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_parameter_tune_carryover, split out of the
test_qa_scenarios monolith (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'ParameterTuneCarryoverTests.test_registered_ahead_of_failover',
    'ParameterTuneCarryoverTests.test_clean_tune_passes_and_validates',
    'ParameterTuneCarryoverTests.test_unsettled_tune_fails',
    'ParameterTuneCarryoverTests.test_unreported_tune_is_inconclusive',
    'ParameterTuneCarryoverTests.test_missing_journal_fails',
    'ParameterTuneCarryoverTests.test_out_of_range_applying_fails',
    'ParameterTuneCarryoverTests.test_reverted_tune_fails',
})


class TuneFeed:
    """A stubbed monitor pair for the parameter-tune carryover
    scenario. ctrl-a owns the field and holds the parameter's live
    tune; ctrl-b tracks it, its served parameter report mirroring the
    last adopted checkpoint. POST /command validates the declared range
    at submission — an out-of-range tune takes the named out_of_range
    rejection receipt on the spot — while an accepted tune applies at
    the next scan boundary and journals its settled receipt. The
    demote/promote leg switches the pair: the promoted peer's report
    serves what the checkpoint carried. Fault flags stage each named
    failure the issue calls out."""

    COMPONENT = 'alarm-1'
    PARAM = 'hysteresis'
    DEFAULT = 0.1
    RANGE = {'min': {'float': 0.0}, 'max': {'float': 10.0}}

    def __init__(self):
        self.tick = 0
        self.tuned = self.DEFAULT    # ctrl-a's live parameter value
        self.carried = self.DEFAULT  # what ctrl-b's checkpoint adopted
        self.role_a = 'active'
        self.promoted = False
        self.receipts = []
        self.journal = []
        self.next_seq = 1
        # Fault injection for the named-failure cases.
        self.hold_settle = False      # the receipt never leaves accepted
        self.never_reflect = False    # the served report never shows it
        self.apply_outside = False    # the out-of-range tune applies
        self.promote_reverts = False  # the promoted peer reverts
        self.drop_journal = False     # the settlement is never journaled

    def _scan(self):
        # One completed scan per request on either endpoint: both peers
        # pace their own loop, and the active's boundary applies an
        # accepted tune into the same fields its checkpoint carries.
        self.tick += 1
        for receipt in self.receipts:
            accepted = receipt['outcome'].get('accepted')
            if accepted and not self.hold_settle \
                    and self.tick >= accepted['apply_tick']:
                tune = receipt['command']['set_parameter']
                self.tuned = tune['value']['float']
                self.carried = self.tuned
                receipt['outcome'] = {'applied': {'tick': self.tick}}
                if not self.drop_journal:
                    self._journal(receipt)

    def _journal(self, receipt):
        self.journal.append({'seq': self.next_seq, 'tick': self.tick,
                             'event': {'command_settled': {
                                 'receipt': json.loads(
                                     json.dumps(receipt))}}})
        self.next_seq += 1

    def _active(self, method, route, body):
        if (method, route) == ('GET', '/role'):
            return 200, {'role': self.role_a, 'tick': self.tick}
        if (method, route) == ('GET', '/schema'):
            return 200, {'publication': self.tick, 'tick': self.tick,
                         'interfaces': [
                             {'name': self.COMPONENT,
                              'interface': {
                                  'version': 1,
                                  'kind': 'managed-latching-alarm',
                                  'measurements': [],
                                  'configuration': [
                                      {'name': self.PARAM,
                                       'kind': 'float',
                                       'range': self.RANGE,
                                       'capability': 'tunable'}],
                                  'state': [], 'commands': [],
                                  'events': []}}]}
        if (method, route) == ('GET', '/snapshot'):
            served = self.DEFAULT if self.never_reflect else self.tuned
            return 200, {'tick': self.tick, 'parameters': [
                {'name': self.COMPONENT,
                 'values': {self.PARAM: {'float': served}}}]}
        if (method, route) == ('GET', '/receipts'):
            return 200, list(self.receipts)
        if (method, route) == ('GET', '/journal'):
            return 200, list(self.journal)
        if (method, route) == ('POST', '/demote'):
            self.role_a = 'standby'
            return 200, {'role': 'demoting', 'tick': self.tick}
        if (method, route) == ('POST', '/command'):
            tune = body['command']['set_parameter']
            value = tune['value']['float']
            if (value < 0.0 or value > 10.0) and not self.apply_outside:
                receipt = {'command': body['command'],
                           'outcome': {'rejected': {'reason': {
                               'out_of_range': {
                                   'component': tune['component'],
                                   'parameter': tune['name'],
                                   'value': tune['value'],
                                   'range': self.RANGE}}}},
                           'actor': body.get('actor')}
                self.receipts.append(receipt)
                if not self.drop_journal:
                    self._journal(receipt)
                return 200, receipt
            # With apply_outside the declared range is never consulted —
            # the defect — and the out-of-range tune queues and applies
            # like an admitted one.
            receipt = {'command': body['command'],
                       'outcome': {'accepted': {
                           'apply_tick': self.tick + 1}},
                       'actor': body.get('actor')}
            self.receipts.append(receipt)
            return 200, receipt
        raise AssertionError('unexpected request %s %s' % (method, url))

    def _peer(self, method, route):
        if (method, route) == ('GET', '/role'):
            if self.promoted:
                return 200, {'role': 'active', 'tick': self.tick}
            return 200, {'role': 'standby', 'tick': self.tick,
                         'sync': {'tracking': {'aligned': self.tick}}}
        if (method, route) == ('GET', '/snapshot'):
            return 200, {'tick': self.tick, 'parameters': [
                {'name': self.COMPONENT,
                 'values': {self.PARAM: {'float': self.carried}}}]}
        if (method, route) == ('POST', '/promote'):
            self.promoted = True
            # The promoted peer's report serves what its adopted
            # checkpoint carried — or the declared default when the
            # tune never crossed.
            self.carried = self.DEFAULT if self.promote_reverts \
                else self.tuned
            return 200, {'role': 'promoting', 'tick': self.tick}
        raise AssertionError('unexpected request %s %s' % (method, url))

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route = path.partition('?')[0]
        self._scan()
        if host == 'ctrl-b:2':
            return self._peer(method, route)
        return self._active(method, route, body)


class ParameterTuneCarryoverTests(unittest.TestCase):
    """scenario_parameter_tune_carryover against the stubbed pair: the
    tune/receipt/report legs, the named out-of-range rejection, the
    cross-promotion continuity assertion, and the inconclusive case
    when the served report never reflects the tune."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = TuneFeed()

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self):
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence)}
        with patch.object(scenarios, 'http_json', self.feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'TUNE_DEADLINE', 0.5):
            return scenarios.scenario_parameter_tune_carryover(ctx)

    def test_registered_ahead_of_failover(self):
        self.assertIn(scenarios.scenario_parameter_tune_carryover,
                      scenarios.SCENARIOS)
        # The tune can only cross a checkpoint from ctrl-a to the
        # tracking ctrl-b, so the case's promotion is the run's one
        # a->b switch — it must run ahead of the failover leg.
        self.assertLess(
            scenarios.SCENARIOS.index(
                scenarios.scenario_parameter_tune_carryover),
            scenarios.SCENARIOS.index(scenarios.scenario_failover))

    def test_clean_tune_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)

    def test_unsettled_tune_fails(self):
        # The in-range submission's receipt never leaves accepted.
        self.feed.hold_settle = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('settled receipt', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unreported_tune_is_inconclusive(self):
        # The receipt settles applied but the served parameter report
        # never reflects the tune — the lane cannot prove the carryover
        # legs, so the case is inconclusive rather than a false pass.
        self.feed.never_reflect = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never reflected', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_journal_fails(self):
        # The tune's settlement never lands in the transition journal.
        self.feed.drop_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal', record.get('detail', ''))
        report.validate_scenario(record)

    def test_out_of_range_applying_fails(self):
        # The out-of-range submission applies instead of meeting the
        # named out_of_range rejection.
        self.feed.apply_outside = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('not rejected by name',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_reverted_tune_fails(self):
        # The promoted peer's report reverts to the model-declared
        # default — tuning loss across promotion is a product defect,
        # not an inconclusive run.
        self.feed.promote_reverts = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('default', record.get('detail', ''))
        report.validate_scenario(record)


if __name__ == '__main__':
    unittest.main()
