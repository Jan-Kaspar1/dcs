"""The 1800_demote_settle_uniqueness leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_demote_settle_uniqueness, split out of the
test_qa_scenarios monolith (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'DemoteSettleTests.test_registered',
    'DemoteSettleTests.test_clean_pair_passes_and_validates',
    'DemoteSettleTests.test_superseded_admission_is_a_legal_single_outcome',
    'DemoteSettleTests.test_double_settled_admission_reports_nondeterministic',
    'DemoteSettleTests.test_vanished_admission_reports_failed',
    'DemoteSettleTests.test_phantom_application_reports_nondeterministic',
    'DemoteSettleTests.test_diverged_logs_report_nondeterministic',
    'DemoteSettleTests.test_refused_demote_reports_failed',
    'DemoteSettleTests.test_refused_promote_reports_failed',
    'DemoteSettleTests.test_no_active_reports_failed',
    'DemoteSettleTests.test_unconverged_pair_reports_inconclusive',
    'DemoteSettleTests.test_unreachable_peer_reports_inconclusive',
    'DemoteSettleTests.test_diverging_digests_report_nondeterministic',
    'DemoteSettleTests.test_two_runs_produce_identical_evidence',
})


class DemoteSettleTests(unittest.TestCase):
    """The demote-settle-uniqueness leg against the stubbed pair: a
    clean rig passes with identical digests and evidence — raced
    admissions settling either legal single outcome — each doctored
    defect reports the named diagnostic, and an unreachable peer is
    inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = DemoteSettleFeed()

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self, feed=None):
        feed = feed or self.feed
        ctx = {'active': 'http://ctrl-a:1',
               'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence)}
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'DEMOTE_SETTLE_SETTLE', 2.0), \
                patch.object(scenarios, 'DEMOTE_SETTLE_AUDIT', 2.0), \
                patch.object(scenarios, 'DEMOTE_SETTLE_POLL', 0.001):
            return scenarios.scenario_demote_settle_uniqueness(ctx)

    def test_registered(self):
        order = list(scenarios.SCENARIOS)
        # The restored pre-switch window behind the standby-loss case —
        # the settled tracking pair ahead of the tune case's a->b
        # switch.
        self.assertLess(
            order.index(scenarios.scenario_standby_loss),
            order.index(scenarios.scenario_demote_settle_uniqueness))
        self.assertEqual(
            order.index(scenarios.scenario_demote_settle_uniqueness)
            + 1,
            order.index(scenarios.scenario_demote_carry_settle))
        self.assertIs(
            verify.case_function('demote-settle-uniqueness'),
            scenarios.scenario_demote_settle_uniqueness)

    def test_clean_pair_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        for name in ('demote-settle-signals.json',
                     'demote-settle-pass-1.json',
                     'demote-settle-pass-2.json'):
            self.assertTrue((self.evidence / name).is_file(), name)
        passes = [json.loads((self.evidence / name).read_text())
                  for name in ('demote-settle-pass-1.json',
                               'demote-settle-pass-2.json')]
        self.assertEqual(passes[0]['digest'], passes[1]['digest'])
        self.assertEqual(passes[0]['digest']['outcomes'], 'single')
        self.assertEqual(passes[0]['digest']['roles'], 'restored')
        report.validate_scenario(record)

    def test_superseded_admission_is_a_legal_single_outcome(self):
        self.feed.drop_carry = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        passed = json.loads(
            (self.evidence / 'demote-settle-pass-1.json').read_text())
        outcomes = {
            scenarios._outcome_key(receipt)
            for audit in passed['audit'] if audit['window']
            for entries in audit['window']['journaled'].values()
            for receipt in entries}
        self.assertIn('rejected:superseded', outcomes, outcomes)
        report.validate_scenario(record)

    def test_double_settled_admission_reports_nondeterministic(self):
        self.feed.double_settle = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'demote-settle-uniqueness-nondeterministic'),
            record['detail'])
        report.validate_scenario(record)

    def test_vanished_admission_reports_failed(self):
        self.feed.drop_admission = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'demote-settle-uniqueness-failed'), record['detail'])
        self.assertIn('terminal journaled outcome', record['detail'])
        report.validate_scenario(record)

    def test_phantom_application_reports_nondeterministic(self):
        self.feed.drop_carry = True
        self.feed.phantom_apply = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'demote-settle-uniqueness-nondeterministic'),
            record['detail'])
        self.assertIn('application the journal never settled',
                      record['detail'])
        report.validate_scenario(record)

    def test_diverged_logs_report_nondeterministic(self):
        self.feed.diverge_logs = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'demote-settle-uniqueness-nondeterministic'),
            record['detail'])
        self.assertIn('adopted log', record['detail'])
        report.validate_scenario(record)

    def test_refused_demote_reports_failed(self):
        self.feed.demote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'demote-settle-uniqueness-failed'), record['detail'])
        self.assertIn('demote', record['detail'])
        report.validate_scenario(record)

    def test_refused_promote_reports_failed(self):
        self.feed.promote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'demote-settle-uniqueness-failed'), record['detail'])
        self.assertIn('promote', record['detail'])
        report.validate_scenario(record)

    def test_no_active_reports_failed(self):
        self.feed.no_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no peer reports role=active', record['detail'])
        report.validate_scenario(record)

    def test_unconverged_pair_reports_inconclusive(self):
        self.feed.no_tracking = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('tracking standby', record['detail'])
        report.validate_scenario(record)

    def test_unreachable_peer_reports_inconclusive(self):
        self.feed.unreachable = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record['detail'])
        report.validate_scenario(record)

    def test_diverging_digests_report_nondeterministic(self):
        passes = iter([({'outcomes': 'single'}, {}, {'pass': 1}),
                       ({'outcomes': 'diverged'}, {}, {'pass': 2})])
        with patch.object(scenarios, '_demote_settle_pass',
                          lambda *a: next(passes)):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'demote-settle-uniqueness-nondeterministic'),
            record['detail'])
        self.assertIn('digests diverged', record['detail'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        runs = []
        for _ in range(2):
            feed = DemoteSettleFeed()
            evidence = Path(self.tmp.name) / ('run' + str(len(runs)))
            evidence.mkdir()
            self.evidence = evidence
            record = self.run_scenario(feed=feed)
            runs.append((record, {p.name: p.read_text()
                                  for p in evidence.iterdir()}))
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()
