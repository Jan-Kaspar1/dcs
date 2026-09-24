import concurrent.futures
import json
import tempfile
import unittest
from pathlib import Path
from agent_pool.state import State


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'state.db'
        self.state = State(self.path)

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def test_atomic_duplicate_claim(self):
        def claim(n):
            state = State(self.path)
            try:
                return state.reserve(1, f'worker-{n}', 'group') is not None
            finally:
                state.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(claim, range(8))), 1)

    def test_dependencies_gate_but_groups_do_not_serialize(self):
        self.assertIsNone(self.state.reserve(2, 'two', 'b', [1]))
        self.assertIsNotNone(self.state.reserve(1, 'one', 'a'))
        self.assertIsNotNone(self.state.reserve(3, 'three', 'a'))
        self.state.complete(1)
        self.assertIsNotNone(self.state.reserve(2, 'two', 'a', [1]))

    def test_repairs_pause_and_retry_conflict(self):
        self.state.reserve(1,'one','a')
        for _ in range(3):
            self.assertTrue(self.state.repair(1,'merge-conflict'))
        self.assertFalse(self.state.repair(1,'merge-conflict'))
        self.state.reserve(2,'one','a')
        self.assertFalse(self.state.retry(1,'worker-failure'))
        self.state.pause()
        self.assertIsNone(self.state.reserve(3,'three','c'))
        self.state.resume()
        self.assertIsNotNone(self.state.reserve(3,'three','c'))
        self.state.integrity('conflicting PRs')
        with self.assertRaises(RuntimeError):
            self.state.resume()

    def test_repair_and_retry_record_one_attributed_row_each(self):
        self.state.reserve(1, 'one', 'a')
        self.assertTrue(self.state.repair(1, 'ci-failure'))
        self.state.update_job(1, status='blocked', error='failed checks')
        self.assertTrue(self.state.retry(1, 'quota-requeue'))
        events = list(reversed(self.state.events(1)))
        self.assertEqual([event['kind'] for event in events],
                         ['reserved', 'repair', 'status:blocked',
                          'retry-reserved', 'redispatch'])
        causes = [json.loads(event['payload']).get('cause') for event in events]
        self.assertEqual(causes, [None, 'ci-failure', None, None, 'quota-requeue'])

    def test_repair_and_retry_reject_unbounded_causes(self):
        self.state.reserve(1, 'one', 'a')
        with self.assertRaises(ValueError):
            self.state.repair(1, 'something-else')
        with self.assertRaises(ValueError):
            self.state.retry(1, 'ci-failure')
        self.assertTrue(self.state.repair(1, 'publish-error'))
        self.assertEqual([event['kind'] for event in self.state.events(1)],
                         ['repair', 'reserved'])

    def test_exhausted_repair_writes_no_attributed_row(self):
        self.state.reserve(1, 'one', 'a')
        for _ in range(3):
            self.assertTrue(self.state.repair(1, 'merge-conflict'))
        self.assertFalse(self.state.repair(1, 'merge-conflict'))
        repairs = [e for e in self.state.events(1) if e['kind'] == 'repair']
        self.assertEqual(len(repairs), 3)
        self.assertEqual(self.state.job(1)['status'], 'blocked')

    def test_merge_flow_reports_repairs_and_redispatches_by_cause(self):
        self.state.reserve(1, 'one', 'a')
        self.state.repair(1, 'merge-conflict')
        self.state.update_job(1, status='blocked', error='x')
        self.state.retry(1, 'worker-failure')
        flow = self.state.merge_flow()
        self.assertEqual(flow['repairs_by_cause']['current'], {'merge-conflict': 1})
        self.assertEqual(flow['redispatches_by_cause']['current'], {'worker-failure': 1})
        self.assertEqual(flow['repairs_by_cause']['previous'], {})

    def test_ramp_and_restart(self):
        for number in range(1,16):
            self.assertIsNotNone(self.state.reserve(number,'one','a'))
            self.state.complete(number)
            self.state.complete(number)
            self.assertEqual(self.state.capacity(),20 if number>=15 else 10 if number>=5 else 5)
        other = State(self.path)
        self.assertEqual(other.get('merges'),15)
        self.assertEqual(other.job(15)['status'],'done')
        other.close()
