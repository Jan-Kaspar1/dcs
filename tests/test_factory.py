"""Contracts at the authoritative factory cycle, using real durable state."""
import json
import time
import unittest
from unittest.mock import Mock
from agent_pool.supervisor import Supervisor
from tests import test_supervisor as fixtures
issue = fixtures.issue


class FactoryTests(unittest.TestCase):
    setUp = fixtures.SupervisorTests.setUp
    tearDown = fixtures.SupervisorTests.tearDown
    kill_worker = fixtures.SupervisorTests.kill_worker
    recoverable = fixtures.SupervisorTests.recoverable

    def target(self, s, target):
        s.admission.groups['swe-2-high']['initial'] = target
        with s.state.db:
            s.state.db.execute("UPDATE admission_groups SET target=? WHERE grp='swe-2-high'", (target,))

    def enable(self):
        self.supervisor.state.close()
        self.config['factory'] = dict(worker_slots=4, workspace_slots=8, daily_merge_goal=50)
        self.supervisor = Supervisor(self.config)
        self.supervisor.github = self.github
        self.supervisor.runtime = self.runtime
        self.supervisor.clock = lambda: 10000
        self.supervisor.admission.clock = self.supervisor.clock
        self.supervisor.admission.jitter = lambda: 0
        self.supervisor.admission.groups['swe-2-high']['ceiling'] = 4
        from pathlib import Path
        self.runtime.pool_root = Path(self.config['pool_root'])
        self.github.update_issue = Mock()
        self.github.comment = Mock()
        return self.supervisor

    def test_waiting_worker_owns_probe_even_when_background_is_due(self):
        s = self.enable()
        s.dispatch(self.github.items)
        self.kill_worker('Rate limit; Retry-After: 30')
        self.recoverable()
        s.clock = lambda: 10031
        s.admission.clock = s.clock
        s.factory.advance(self.github.items, [])
        self.assertEqual(s.state.job(1)['status'], 'working')
        self.assertIsNone(s.state.get('planner'))
        self.assertIsNone(s.state.get('reviewer'))
        self.assertEqual([r[0] for r in s.state.db.execute('SELECT owner FROM admission_leases')], ['job:1'])

    def test_quota_wait_never_exhausts_work_and_survives_restart(self):
        s = self.enable()
        s.admission.max_quota_requeues = 0
        s.dispatch(self.github.items)
        self.kill_worker('Rate limit; Retry-After: 30')
        self.recoverable()
        self.assertEqual(s.state.get('retry:1'), 'quota-requeue')
        restarted = Supervisor(self.config)
        restarted.github, restarted.runtime = self.github, self.runtime
        restarted.clock = lambda: 10031
        restarted.admission.clock = restarted.clock
        restarted.admission.jitter = lambda: 0
        try:
            self.assertTrue(restarted.state.get('recovery:1')['provider_wait'])
            restarted.factory.advance(self.github.items, [])
            self.assertEqual(restarted.state.job(1)['status'], 'working')
        finally:
            restarted.state.close()

    def test_quota_wait_does_not_run_early_or_reopen_auth_block(self):
        s = self.enable()
        s.dispatch(self.github.items)
        self.kill_worker('Rate limit; Retry-After: 30')
        self.recoverable()
        s.factory.advance(self.github.items, [])
        self.assertEqual(s.state.job(1)['status'], 'blocked')
        s.clock = lambda: 10031
        s.admission.clock = s.clock
        s.admission.finish('external', {'model':'swe-2-high'}, 'auth')
        s.factory.advance(self.github.items, [])
        self.assertEqual(s.state.job(1)['status'], 'blocked')
        self.assertEqual(s.admission.summary()['groups']['swe-2-high']['mode'], 'blocked')

    def test_completed_inference_releases_capacity_during_publish_outage(self):
        s = self.enable()
        s.dispatch(self.github.items)
        self.github.fail_publish = True
        s.factory.advance(self.github.items, [])
        self.assertEqual(s.admission.summary()['leases'], 0)
        self.assertTrue(s.state.get('delivery:1'))
        self.assertEqual(s.state.job(1)['status'], 'working')
        self.assertEqual(self.runtime.spawn.call_count, 1)
        self.github.fail_publish = False
        s.factory.advance(self.github.items, [])
        self.assertEqual(self.runtime.spawn.call_count, 1)
        self.assertEqual(s.state.job(1)['status'], 'pr-open')

    def test_closed_retry_and_dependency_cannot_launch(self):
        s = self.enable()
        s.dispatch(self.github.items)
        self.kill_worker('Rate limit; Retry-After: 30')
        self.recoverable()
        self.github.items[0]['state'] = 'CLOSED'
        self.github.items.append(issue(2, dependencies=(9,)))
        s.clock = lambda: 10031
        s.admission.clock = s.clock
        s.factory.advance(self.github.items, [])
        self.assertEqual([r[0] for r in s.state.db.execute("SELECT owner FROM admission_leases WHERE owner LIKE 'job:%'")], [])
        self.assertIsNone(s.state.job(2))

    def test_delivery_repair_is_queued_and_wins_over_new_work(self):
        s = self.enable()
        s.dispatch(self.github.items)
        s.reconcile_workers(self.github.items)
        self.github.items.append(issue(2))
        self.github.checks = {'test':'failure'}
        s.integrate(self.github.items)
        self.assertEqual(self.runtime.spawn.call_count, 1)
        self.assertTrue(s.state.get('repair:1'))
        self.target(s, 1)
        s.factory.advance(self.github.items, [])
        self.assertEqual(self.runtime.spawn.call_count, 2)
        self.assertEqual(s.state.job(1)['status'], 'working')
        self.assertIsNone(s.state.job(2))

    def test_four_inference_slots_are_separate_from_ci_workspaces(self):
        s = self.enable()
        self.github.items = [issue(n) for n in range(1, 7)]
        self.target(s, 4)
        self.runtime.poll.return_value = None
        s.factory.advance(self.github.items, [])
        self.assertEqual(s.admission.summary()['leases'], 4)
        self.runtime.poll.return_value = {'exit_code':0}
        self.github.checks = {'test':'pending'}
        s.factory.advance(self.github.items, [])
        self.assertEqual(len(s.state.jobs(('pr-open',))), 4)
        self.assertEqual(len(s.state.jobs(('working',))), 2)
        self.assertEqual(s.admission.summary()['leases'], 2)

    def test_background_failure_does_not_block_worker_or_hide_failure(self):
        s = self.enable()
        s.review = Mock(side_effect=ValueError('invalid review report'))
        s.factory.advance(self.github.items, [])
        self.assertEqual(s.state.job(1)['status'], 'working')
        self.assertIn('review', s.state.get('factory:status')['errors'])

    def test_inventory_outage_reconciles_receipt_without_new_assignment(self):
        s = self.enable()
        self.github.items.append(issue(2))
        self.runtime.poll.return_value = None
        self.target(s, 1)
        s.factory.advance(self.github.items, [])
        self.runtime.poll.return_value = {'exit_code':0}
        self.github.fail_publish = True
        s.factory.offline()
        self.assertEqual(s.admission.summary()['leases'], 0)
        self.assertEqual(self.runtime.spawn.call_count, 1)
        self.assertIsNone(s.state.job(2))
        self.assertTrue(s.state.get('delivery:1'))

    def test_one_publication_outage_does_not_hold_other_completed_leases(self):
        s = self.enable()
        self.github.items.append(issue(2))
        self.runtime.poll.return_value = None
        s.factory.advance(self.github.items, [])
        self.runtime.poll.return_value = {'exit_code':0}
        self.github.fail_publish = True
        s.factory.advance(self.github.items, [])
        self.assertEqual(s.admission.summary()['leases'], 0)
        self.assertTrue(s.state.get('delivery:1'))
        self.assertTrue(s.state.get('delivery:2'))

    def test_queued_repair_cannot_block_green_pr_during_quota_wait(self):
        s = self.enable()
        self.github.items.append(issue(2))
        s.dispatch(self.github.items)
        s.reconcile_workers(self.github.items)
        s.repair(s.state.job(1), self.github.items[0], 'CI failed', 'ci-failure')
        s.admission.finish('external', {'model':'swe-2-high'}, 'rate', 60)
        merges = []
        self.github.merge = lambda n, required: merges.append(n) or True
        s.factory.advance(self.github.items, [])
        self.assertEqual(merges, [s.state.job(2)['pr']])
        self.assertEqual(self.runtime.spawn.call_count, 2)

    def test_quota_resume_keeps_semantic_repair_budget(self):
        s = self.enable()
        s.dispatch(self.github.items)
        s.state.update_job(1, repairs=2)
        self.kill_worker('Rate limit; Retry-After: 30')
        self.recoverable()
        s.clock = lambda: 10031
        s.admission.clock = s.clock
        s.factory.advance(self.github.items, [])
        self.assertEqual(s.state.job(1)['repairs'], 2)
        self.assertEqual(s.state.job(1)['status'], 'working')
