import concurrent.futures
import json
import tempfile
import unittest
from pathlib import Path
from agent_pool.state import State
from scripts import merge_flow


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

    def test_merge_flow_ranks_conflict_paths_with_shared_verdict(self):
        for number in range(1, 4):
            self.state.reserve(number, f'worker-{number}', 'a')
        self.state.repair(1, 'merge-conflict',
                          detail={'paths': ['docs/plan.md', 'crates/x.rs']})
        self.state.repair(2, 'merge-conflict', detail={'paths': ['docs/plan.md']})
        self.state.repair(3, 'merge-conflict')
        flow = self.state.merge_flow()
        self.assertEqual(flow['conflict_repairs']['current'], 3)
        self.assertEqual(flow['conflict_paths']['current'],
                         {'docs/plan.md': 2, 'crates/x.rs': 1, 'unclassified': 1})
        self.assertEqual(flow['conflict_load']['current'], 'concentrated')
        self.assertEqual(flow['conflict_paths']['previous'], {})
        self.assertEqual(flow['conflict_load']['previous'], 'none')

    def test_merge_flow_attributes_conflict_paths_by_resolution_class(self):
        # The planner summary carries the same classes the standalone report
        # does, so a coverage decision reads one vocabulary.
        self.state.reserve(1, 'one', 'a')
        self.state.repair(1, 'merge-conflict',
                          detail={'paths': ['docs/lenovo-hardware-qa-plan.md']})
        self.state.record_event('mechanical-resolution', issue=2, attempt=1,
                                payload={'resolvers': {
                                    'docs/lenovo-hardware-qa-plan.md':
                                        'qa-plan-landed-union'}})
        self.state.reserve(3, 'three', 'a')
        self.state.repair(3, 'merge-conflict',
                          detail={'paths': ['docs/architecture.md']})
        flow = self.state.merge_flow()
        self.assertEqual(flow['conflict_paths_by_resolution']['current'], {
            'resolved': {'docs/lenovo-hardware-qa-plan.md': 1},
            'registered_unresolved': {'docs/lenovo-hardware-qa-plan.md': 1},
            'unregistered': {'docs/architecture.md': 1}})
        self.assertEqual(flow['mechanical_resolutions']['current'], 1)
        self.assertEqual(flow['repairs_on_registered_paths']['current'], 1)
        self.assertEqual(flow['conflict_paths_by_resolution']['previous'],
                         {'resolved': {}, 'registered_unresolved': {},
                          'unregistered': {}})
        self.assertEqual(flow['repairs_on_registered_paths']['previous'], 0)

    def test_merge_flow_conflict_load_spread_and_unattributed(self):
        for number in range(1, 6):
            self.state.reserve(number, f'worker-{number}', 'a')
            self.state.repair(number, 'merge-conflict',
                              detail={'paths': [f'crates/c{number}.rs']})
        flow = self.state.merge_flow()
        self.assertEqual(flow['conflict_paths']['current'],
                         {f'crates/c{number}.rs': 1 for number in range(1, 6)})
        self.assertEqual(flow['conflict_load']['current'], 'spread')
        with tempfile.TemporaryDirectory() as tmp:
            other = State(Path(tmp) / 'state.db')
            try:
                for number in (1, 2):
                    other.reserve(number, f'worker-{number}', 'a')
                    other.repair(number, 'merge-conflict')
                flow = other.merge_flow()
            finally:
                other.close()
        self.assertEqual(flow['conflict_paths']['current'], {'unclassified': 2})
        self.assertEqual(flow['conflict_load']['current'], 'unattributed')

    def test_merge_flow_names_failing_checks_from_payload_or_job_record(self):
        self.state.reserve(1, 'one', 'a')
        self.state.repair(1, 'ci-failure', detail={'checks': ['verify', 'clippy']})
        self.state.reserve(2, 'two', 'a')
        self.state.repair(2, 'ci-failure')
        self.state.update_job(2, status='blocked',
                              error='Repair limit exhausted: CI failed. Inspect gh pr '
                                    'checks and gh run view --log-failed as read-only '
                                    'diagnostics. {"verify": "failure"}')
        self.state.reserve(3, 'three', 'a')
        self.state.repair(3, 'ci-failure')
        flow = self.state.merge_flow()
        self.assertEqual(flow['failing_checks']['current'],
                         {'verify': 2, 'clippy': 1, 'unclassified': 1})

    def test_merge_flow_parks_classify_into_shared_vocabulary(self):
        self.state.reserve(1, 'one', 'a')
        self.state.update_job(1, status='blocked',
                              error='Local agent failed: {"status": "timeout", "exit_code": -15}')
        flow = self.state.merge_flow()
        self.assertEqual(set(flow['park_causes']['current']), set(merge_flow.PARK_CAUSES))
        self.assertEqual(flow['parked']['current'], 1)
        self.assertEqual(flow['park_causes']['current']['timeout'], 1)
        self.assertEqual(flow['parked']['previous'], 0)
        self.assertEqual(flow['park_causes']['previous'],
                         {cause: 0 for cause in merge_flow.PARK_CAUSES})

    def test_merge_flow_carries_dispatch_merge_and_backlog_counts(self):
        # Previous window: issue 1 parks and stays blocked, issue 2 merges.
        self.state.reserve(1, 'one', 'a')
        self.state.update_job(1, status='blocked', error='x')
        self.state.reserve(2, 'two', 'a')
        self.state.complete(2)
        with self.state.db:
            self.state.db.execute(
                'UPDATE work_events SET at=at-? WHERE issue IN (1,2)', (10 * 86400,))
            self.state.db.execute(
                'UPDATE jobs SET updated=updated-? WHERE issue IN (1,2)', (10 * 86400,))
        # Current window: issue 3 parks, redispatches, parks again;
        # issue 4 merges; issue 5 is dispatched and still working.
        self.state.reserve(3, 'three', 'a')
        self.state.update_job(3, status='blocked', error='y')
        self.state.retry(3, 'quota-requeue')
        self.state.update_job(3, status='blocked', error='z')
        self.state.reserve(4, 'four', 'a')
        self.state.complete(4)
        self.state.reserve(5, 'five', 'a')
        flow = self.state.merge_flow()
        self.assertEqual(flow['dispatches']['previous'], 2)
        self.assertEqual(flow['first_dispatches']['previous'], 2)
        self.assertEqual(flow['retry_dispatches']['previous'], 0)
        self.assertEqual(flow['merged_and_closed']['previous'], 1)
        self.assertEqual(flow['still_blocked']['previous'], 1)
        self.assertEqual(flow['dispatches']['current'], 4)
        self.assertEqual(flow['first_dispatches']['current'], 3)
        self.assertEqual(flow['retry_dispatches']['current'], 1)
        self.assertEqual(flow['merged_and_closed']['current'], 1)
        self.assertEqual(flow['still_blocked']['current'], 2)

    def test_merge_flow_reports_zero_counts_on_empty_ledger(self):
        flow = self.state.merge_flow()
        for window in ('current', 'previous'):
            self.assertEqual(flow['dispatches'][window], 0)
            self.assertEqual(flow['first_dispatches'][window], 0)
            self.assertEqual(flow['retry_dispatches'][window], 0)
            self.assertEqual(flow['merged_and_closed'][window], 0)
            self.assertEqual(flow['still_blocked'][window], 0)

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
