"""The 3300_command_admission leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_command_admission, split out of the
test_qa_scenarios monolith (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'CommandAdmissionTests.test_registered_in_scenarios',
    'CommandAdmissionTests.test_over_limit_mixed_settle_passes',
    'CommandAdmissionTests.test_under_limit_all_admitted_fails_named_rejection',
    'CommandAdmissionTests.test_dropped_receipt_fails',
    'CommandAdmissionTests.test_http_layer_error_fails',
    'CommandAdmissionTests.test_unsettled_admission_fails',
    'CommandAdmissionTests.test_bounded_receipt_window_still_audits',
})


class CommandAdmissionTests(unittest.TestCase):
    """scenario_command_admission against the stubbed feed: the flood
    channel's pipelined submissions fill the fake's bounded queue inside
    one scan window, so the over-limit leg shows the mixed
    settle/reject shape and the under-limit leg all-admitted."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = Feed()

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self):
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence)}
        with patch.object(scenarios, 'http_json', self.feed.http_json), \
                patch.object(scenarios, '_connect',
                             self.feed.flood_connect), \
                patch.object(scenarios, 'LEG_TICKS', 4), \
                patch.object(scenarios, 'LEG_POLL', 0.001), \
                patch.object(scenarios, 'LEG_DEADLINE', 2.0), \
                patch.object(scenarios, 'ADMISSION_TRICKLE', 3):
            return scenarios.scenario_command_admission(ctx)

    def test_registered_in_scenarios(self):
        self.assertIn(scenarios.scenario_command_admission,
                      scenarios.SCENARIOS)

    def test_over_limit_mixed_settle_passes(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        # The over-limit leg: admitted receipts settled applied at the
        # boundary and the named queue_full rejection appeared.
        self.assertTrue(self.feed.flood_pending)
        self.assertGreater(self.feed.full_rejections, 0)
        flood = json.loads(
            (self.evidence / 'command-admission-flood.json').read_text())
        self.assertEqual(flood['capacity'], self.feed.capacity)
        self.assertGreater(flood['queue_full'], 0)

    def test_under_limit_all_admitted_fails_named_rejection(self):
        # The under-limit leg: every submission is admitted and settles
        # applied — and the scenario names the missing queue_full.
        self.feed.bounded = False
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('queue_full rejection never appeared',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_dropped_receipt_fails(self):
        self.feed.flood_drop = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no receipt', record.get('detail', ''))
        report.validate_scenario(record)

    def test_http_layer_error_fails(self):
        self.feed.flood_status = 500
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('HTTP-layer', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unsettled_admission_fails(self):
        self.feed.hold_flood = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never settled applied', record.get('detail', ''))
        report.validate_scenario(record)

    def test_bounded_receipt_window_still_audits(self):
        # The qa-20260917-035 reproduction: the receipt log's bounded
        # tail rolls under the flood — settled receipts evict while
        # submissions still land — so the settlement audit correlates
        # absolute submission indexes against the window's high-water,
        # not positions in the tail.
        self.feed.receipt_cap = 8
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertGreater(self.feed.attempts, len(self.feed.receipts))
        report.validate_scenario(record)


if __name__ == '__main__':
    unittest.main()
