import json
import unittest
from pathlib import Path

from qa_lane import relay


FIXTURES = Path(__file__).resolve().parents[1] / 'qa_lane' / 'fixtures'


class SanitizeTests(unittest.TestCase):
    def test_fixture_reports_sanitize_and_revalidate(self):
        for name in ('report-passed.json', 'report-failed.json',
                     'report-interrupted.json'):
            doc = relay.sanitize((FIXTURES / name).read_text())
            self.assertEqual(doc['schema_version'], 1, name)

    def test_redacts_host_paths_users_and_ips(self):
        text = (FIXTURES / 'report-passed.json').read_text()
        doc = json.loads(text)
        doc['notes'] = ('report at /srv/dcs-hwtest/runs/x/report.json '
                        'by jan-kaspar from 192.168.178.107')
        doc['infrastructure_failures'] = [
            {'key': 'disk', 'detail': '/srv/dcs-hwtest nearly full',
             'phase': 'preflight'}]
        clean = relay.sanitize(json.dumps(doc))
        blob = json.dumps(clean)
        self.assertNotIn('/srv/', blob)
        self.assertNotIn('jan-kaspar', blob)
        self.assertNotIn('192.168.', blob)
        self.assertIn('<path>', blob)
        self.assertIn('<user>', blob)
        self.assertIn('<lan-ip>', blob)

    def test_host_is_collapsed_to_lane_identity(self):
        doc = relay.sanitize((FIXTURES / 'report-passed.json').read_text())
        self.assertEqual(doc['host'], {'name': 'lenovo', 'os': 'ubuntu'})

    def test_rejects_invalid_report(self):
        with self.assertRaises(ValueError):
            relay.sanitize('{"schema_version": 99}')


class SummaryTests(unittest.TestCase):
    def test_summary_shape(self):
        doc = relay.sanitize((FIXTURES / 'report-passed.json').read_text())
        summ = relay.summary(doc)
        self.assertEqual(summ['schema'], 'qa-summary')
        self.assertEqual(summ['run_id'], doc['run_id'])
        self.assertEqual(summ['outcome'], 'passed')
        self.assertEqual(summ['scenarios']['failover'], 'passed')
        self.assertEqual(summ['capability_limitations'], ['no-ethercat'])
        self.assertEqual(summ['infrastructure_failures'], [])
        # summary must fit the receiver bound comfortably
        self.assertLess(len(json.dumps(summ)), 8192)

    def test_doc_names_match_receiver_pattern(self):
        doc = relay.sanitize((FIXTURES / 'report-passed.json').read_text())
        for name in ('latest', 'run-' + doc['run_id']):
            self.assertRegex(name, r'^[a-z0-9][a-z0-9-]{0,79}$')


if __name__ == '__main__':
    unittest.main()
