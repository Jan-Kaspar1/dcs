import unittest
from agent_pool import scheduling
from tests.test_supervisor import issue


class SchedulingTests(unittest.TestCase):
    def test_inventory_explains_ready_dependency_and_existing_job(self):
        ready = issue(1)
        waiting = issue(2, dependencies=(9,))
        working = issue(3)
        rows = scheduling.inventory(
            [ready, waiting, working],
            [{'issue': 3, 'status': 'working'}],
        )
        by_issue = {row['issue']: row for row in rows}
        self.assertEqual(by_issue[1]['reason'], 'ready')
        self.assertEqual(by_issue[2]['reason'], 'dependency')
        self.assertEqual(by_issue[2]['missing_dependencies'], [9])
        self.assertEqual(by_issue[3]['reason'], 'working')

    def test_retries_count_as_demand_for_early_planning(self):
        self.assertFalse(scheduling.due_for_planning(1000, 0, 0, 6))
        self.assertTrue(scheduling.due_for_planning(1000, 0, 0, 5))
        self.assertFalse(scheduling.due_for_planning(7300, 0, 0, 6))
        self.assertTrue(scheduling.due_for_planning(100, 0, 6, 0, forced=True))

    def test_explain_keeps_target_separate_from_probe_limit(self):
        admission = {'groups': {'swe-2-high': {
            'target': 5, 'mode': 'probing', 'active': 1,
            'external_slots': 0, 'cooldown_until': None}}}
        admission['groups']['retired-model'] = {
            'target': 1, 'mode': 'normal', 'active': 0,
            'external_slots': 0, 'cooldown_until': None}
        view = scheduling.explain([issue(1)], [], admission, [87], now=100,
                                  configured_groups={'swe-2-high'})
        self.assertNotIn('retired-model', view['provider_groups'])
        self.assertEqual(view['work_counts']['ready'], 1)
        self.assertEqual(view['provider_groups']['swe-2-high']['target'], 5)
        self.assertEqual(view['provider_groups']['swe-2-high']['limiting_reason'], 'single-probe')
        self.assertEqual(view['armed_retries'], [87])


if __name__ == '__main__':
    unittest.main()
