import json
import tempfile
import unittest
from pathlib import Path

from agent_pool.admission import Admission
from agent_pool.state import State


class WorkLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name) / 'state.sqlite3')

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def test_job_transitions_are_durable_and_retry_is_not_duplicated(self):
        self.state.reserve(7, 'worker-01', 'dcs-core')
        self.state.update_job(7, status='blocked', error='test failure')
        self.assertTrue(self.state.retry(7, 'worker-failure'))
        self.assertFalse(self.state.retry(7, 'worker-failure'))
        self.state.update_job(7, status='pr-open', pr=11)
        self.state.complete(7)
        events = list(reversed(self.state.events(7)))
        self.assertEqual([event['kind'] for event in events], [
            'reserved', 'status:blocked', 'retry-reserved', 'redispatch',
            'status:pr-open', 'merged-and-closed'])
        self.assertEqual(json.loads(events[3]['payload'])['cause'], 'worker-failure')
        self.assertEqual(events[-1]['attempt'], 2)

    def test_invocation_outcome_is_recorded_once(self):
        config = {'models': ['swe-2-high'], 'model_caps': {'swe-2-high': 2}}
        admission = Admission(self.state, config, clock=lambda: 10, jitter=lambda: 0)
        self.assertTrue(admission.reserve('job:7', 'swe-2-high', 'worker-01', 5))
        meta = {'invocation': 'run-7-a', 'started_at': 10}
        admission.attach('job:7', meta)
        admission.finish('job:7', meta, 'rate', retry_after=20)
        admission.finish('job:7', meta, 'rate', retry_after=20)
        events = self.state.events(7)
        self.assertEqual([event['kind'] for event in events], ['invocation:rate'])

    def test_merge_flow_compares_adjacent_windows(self):
        now = 20 * 86400
        for number, age_days in ((1, 2), (2, 9), (3, 10)):
            self.state.reserve(number, f'worker-{number:02d}', 'test')
            self.state.complete(number)
            with self.state.db:
                self.state.db.execute('UPDATE jobs SET updated=? WHERE issue=?',
                                      (now - age_days * 86400, number))
        flow = self.state.merge_flow(now=now)
        self.assertEqual(flow['current_merges'], 1)
        self.assertEqual(flow['previous_merges'], 2)
        self.assertEqual(flow['decline_percent'], 50.0)

    def test_source_key_deduplicates_manual_event(self):
        self.state.record_event('checkpoint', 7, source_key='checkpoint:7:1')
        self.state.record_event('checkpoint', 7, source_key='checkpoint:7:1')
        self.assertEqual(len(self.state.events(7)), 1)


if __name__ == '__main__':
    unittest.main()
