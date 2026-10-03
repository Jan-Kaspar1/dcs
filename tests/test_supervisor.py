import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from agent_pool import planning
from agent_pool.github import GitHubError
from agent_pool.supervisor import Supervisor


def issue(number=1, priority=2, group='core', dependencies=(), area='control-runtime'):
    item = dict(key=f'issue-{number}',title=f'Task {number}',scope='Implement a simulation',
                acceptance='Test passes',tests='unit test',dependencies=list(dependencies),
                priority=priority,milestone='foundation',group=group,area=area)
    return dict(number=number,title=item['title'],body=planning.body(item),
                state='OPEN',labels=[{'name':'agent:ready'}])


class FakeGitHub:
    def __init__(self):
        self.items = [issue()]
        self.pull_requests = {}
        self.checks = {'test':'success'}
        self.fail_publish = False
        self.created = 0

    def issue(self, number):
        return next(i for i in self.items if i['number'] == number)

    def find_pr(self, branch):
        return next((p for p in self.pull_requests.values() if p['branch'] == branch),None)

    def create_pr(self, branch, title, body):
        if self.fail_publish:
            raise GitHubError('HTTP 503')
        self.created += 1
        self.pull_requests[self.created] = dict(number=self.created,branch=branch,state='open',
                merged=False,head={'sha':'head'},base={'ref':'main'})
        return self.created

    def pr(self, number):
        return self.pull_requests[number]

    def includes_main(self, head, base):
        return True

    def check_states(self, sha):
        return self.checks

    def checks_pass(self, pr, required):
        return all(self.checks.get(n) == 'success' for n in required)

    def merge(self, number, required):
        self.pull_requests[number]['merged'] = True
        self.pull_requests[number]['state'] = 'closed'
        self.items[0]['state'] = 'CLOSED'
        return True


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = dict(state_root=str(Path(self.tmp.name)/'state'),
                           pool_root=str(Path(self.tmp.name)/'pool'),repository='fake/repo',
                           timeout_seconds=7200,required_checks=['test'],poll_seconds=60)
        self.supervisor = Supervisor(self.config)
        self.github = FakeGitHub()
        self.runtime = Mock()
        self.runtime.prepare_clone.side_effect=lambda worker, **kw: Path(self.config['pool_root'])/worker
        self.runtime.spawn.side_effect=lambda key,*a,**kw: {'invocation':key,'key':key,'started_at':0}
        self.runtime.poll.return_value={'exit_code':0}
        self.runtime.inspect_result.return_value={'clean':True,'changed':True}
        self.runtime.run_git.return_value='base'
        self.runtime.session_id.return_value='session-one'
        self.runtime.recover.return_value=[]
        self.supervisor.github=self.github
        self.supervisor.runtime=self.runtime

    def tearDown(self):
        self.supervisor.state.close()
        self.tmp.cleanup()

    def attributed_events(self, number):
        return [(e['kind'], json.loads(e['payload']).get('cause'))
                for e in self.supervisor.state.events(number)
                if e['kind'] in ('repair', 'redispatch')]

    def kill_worker(self, tail_text, number=1):
        """Finish `number`'s invocation on a nonzero receipt carrying `tail_text`."""
        self.runtime.poll.return_value = {'exit_code': 1}
        log = Path(self.tmp.name) / 'invocation.log'
        log.write_text(tail_text)
        record = self.supervisor.state.get('process:' + str(number))
        record['log'] = str(log)
        self.supervisor.state.set('process:' + str(number), record)
        self.supervisor.reconcile_workers(self.github.items)

    def recoverable(self, number=1):
        """Make the recorded recovery state dispatchable, keeping its requeue."""
        rec = self.supervisor.state.get('recovery:' + str(number))
        rec.update(work=False, phase='captured')
        self.supervisor.state.set('recovery:' + str(number), rec)
        return rec

    def test_research_issue_worker_prompt_defines_research_role(self):
        research_issue = issue(group='docs/research')
        text = self.supervisor.worker_prompt(research_issue, 'codex/issue-1-1')
        self.assertIn('act as the product research worker', text)
        self.assertIn('separate source facts from proposed DCS behavior', text)
        self.assertIn('does not implement vendor-derived product behavior', text)
        self.assertIn('never edit files outside this checkout', text)

    def test_full_issue_pr_merge_closed_flow(self):
        s=self.supervisor
        s.dispatch(self.github.items)
        self.assertEqual(s.state.job(1)['status'],'working')
        s.reconcile_workers(self.github.items)
        self.assertEqual(s.state.job(1)['status'],'pr-open')
        self.assertEqual(s.state.job(1)['session'],'session-one')
        s.integrate(self.github.items)
        s.integrate(self.github.items)
        self.assertEqual(s.state.job(1)['status'],'done')
        self.assertEqual(s.state.get('merges'),1)
        s.dispatch(self.github.items)
        self.assertEqual(self.github.created,1)

    def test_dispatch_defaults_to_swe_2_high(self):
        self.supervisor.dispatch(self.github.items)
        self.assertEqual(self.runtime.spawn.call_args.kwargs['model'], 'swe-2-high')

    def test_dispatch_rotates_models_by_worker_slot(self):
        self.supervisor.models = ['swe-2-high', 'opencode/union-alpha']
        self.supervisor.dispatch(self.github.items)
        self.assertEqual(self.runtime.spawn.call_args.kwargs['model'], 'opencode/union-alpha')

    def test_dispatch_skips_workers_whose_model_is_capped(self):
        self.supervisor.models = ['swe-2-high', 'opencode/union-alpha']
        self.supervisor.model_caps = {'opencode/union-alpha': 1}
        self.github.items = [issue(1), issue(2), issue(3)]
        self.supervisor.dispatch(self.github.items)
        workers = sorted(j['worker'] for j in self.supervisor.state.jobs())
        self.assertEqual(workers, ['worker-01', 'worker-02', 'worker-04'])

    def test_supervisor_commits_completed_edits(self):
        self.supervisor.dispatch(self.github.items)
        self.runtime.inspect_result.side_effect = [{'clean':False,'changed':False}, {'clean':True,'changed':True}]
        self.supervisor.reconcile_workers(self.github.items)
        self.assertEqual(self.supervisor.state.job(1)['status'], 'pr-open')
        self.assertTrue(any(call.args[1:3] == ('add','--all') for call in self.runtime.run_git.call_args_list))

    def test_zero_exit_permission_denial_blocks_without_repair(self):
        self.supervisor.dispatch(self.github.items)
        log = Path(self.tmp.name) / 'denied.log'
        log.write_text('warning: rejected a tool call that requires confirmation')
        record = self.supervisor.state.get('process:1')
        record['log'] = str(log)
        self.supervisor.state.set('process:1', record)
        self.supervisor.reconcile_workers(self.github.items)
        self.assertEqual(self.supervisor.state.job(1)['status'], 'blocked')
        self.assertEqual(self.supervisor.state.job(1)['repairs'], 0)
        self.assertEqual(self.github.created, 0)

    def test_restart_reconciles_success_receipt(self):
        self.supervisor.dispatch(self.github.items)
        self.supervisor.state.close()
        self.supervisor=Supervisor(self.config)
        self.supervisor.github=self.github
        self.supervisor.runtime=self.runtime
        self.supervisor.reconcile_workers(self.github.items)
        self.assertEqual(self.supervisor.state.job(1)['status'],'pr-open')

    def test_paused_completion_publishes_but_cannot_merge(self):
        s=self.supervisor
        s.dispatch(self.github.items)
        s.state.pause()
        s.reconcile_workers(self.github.items)
        s.integrate(self.github.items)
        self.assertEqual(s.state.job(1)['status'],'pr-open')
        self.assertFalse(self.github.pr(1)['merged'])

    def test_transient_publish_error_preserves_receipt(self):
        s=self.supervisor
        s.dispatch(self.github.items)
        self.github.fail_publish=True
        with self.assertRaises(GitHubError):
            s.reconcile_workers(self.github.items)
        self.assertIsNotNone(s.state.get('process:1'))
        self.github.fail_publish=False
        s.reconcile_workers(self.github.items)
        self.assertEqual(s.state.job(1)['status'],'pr-open')
        self.assertEqual(s.state.job(1)['repairs'],0)

    def test_wrong_branch_publish_error_repairs_without_wedging(self):
        s=self.supervisor
        self.github.items=[issue(1,group='g1'),issue(2,group='g2')]
        s.dispatch(self.github.items)
        clones={j['issue']:j['clone'] for j in s.state.jobs(('working',))}
        def inspect(cwd,branch):
            if str(cwd)==clones[1]:
                raise ValueError('Worker changed the assigned branch')
            return {'clean':True,'changed':True}
        self.runtime.inspect_result.side_effect=inspect
        s.reconcile_workers(self.github.items)
        self.assertEqual(s.state.job(1)['status'],'working')
        self.assertEqual(s.state.job(1)['repairs'],1)
        self.assertIn('Worker changed the assigned branch',self.runtime.spawn.call_args.args[2])
        self.assertEqual(s.state.job(2)['status'],'pr-open')
        s.reconcile_workers(self.github.items)
        self.assertEqual(s.state.job(1)['repairs'],2)

    def test_same_group_issues_dispatch_in_parallel_by_priority(self):
        self.github.items=[issue(1,3),issue(2,0)]
        self.supervisor.dispatch(self.github.items)
        self.assertIsNotNone(self.supervisor.state.job(1))
        self.assertIsNotNone(self.supervisor.state.job(2))
        self.assertIn('Task 2', self.runtime.spawn.call_args_list[0].args[2])

    def test_equal_priority_favors_underinvested_area(self):
        completed = issue(3, area='engineering')
        completed['state'] = 'CLOSED'
        self.github.items = [issue(1, area='engineering'),
                             issue(2, area='library'), completed]
        self.supervisor.state.reserve(3, 'worker-20', 'g')
        self.supervisor.state.complete(3)
        self.supervisor.dispatch(self.github.items)
        self.assertIn('Task 2', self.runtime.spawn.call_args_list[0].args[2])
        summary = self.supervisor.state.get('area_allocation')
        self.assertEqual(summary['library']['active'], 1)

    def test_failed_ci_repairs_with_session(self):
        s=self.supervisor
        s.dispatch(self.github.items)
        s.reconcile_workers(self.github.items)
        self.github.checks={'test':'failure'}
        s.integrate(self.github.items)
        self.assertEqual(s.state.job(1)['repairs'],1)
        self.assertEqual(self.runtime.spawn.call_args.kwargs['resume_session'],'session-one')
        self.assertIn(('repair','ci-failure'), self.attributed_events(1))

    def test_merge_conflict_repair_records_merge_conflict_cause(self):
        s=self.supervisor
        s.dispatch(self.github.items)
        s.reconcile_workers(self.github.items)
        self.github.includes_main=lambda head, base: False
        def run_git(cwd, *args):
            if args and args[0] == 'merge':
                raise subprocess.CalledProcessError(1, 'merge', '', 'conflict')
            return ''
        self.runtime.run_git.side_effect=run_git
        s.integrate(self.github.items)
        self.assertEqual(s.state.job(1)['repairs'],1)
        self.assertIn(('repair','merge-conflict'), self.attributed_events(1))

    def test_merge_conflict_repair_persists_conflicted_paths(self):
        s=self.supervisor
        s.dispatch(self.github.items)
        s.reconcile_workers(self.github.items)
        self.github.includes_main=lambda head, base: False
        out = ("Auto-merging docs/plan.md\n"
               "CONFLICT (content): Merge conflict in docs/plan.md\n"
               "Auto-merging agent_pool/state.py\n"
               "CONFLICT (content): Merge conflict in agent_pool/state.py\n"
               "Automatic merge failed; fix conflicts and then commit the result.\n")
        def run_git(cwd, *args):
            if args and args[0] == 'merge':
                raise subprocess.CalledProcessError(1, 'merge', out, 'error: conflict')
            return 'base'
        self.runtime.run_git.side_effect=run_git
        s.integrate(self.github.items)
        self.assertEqual(s.state.job(1)['repairs'],1)
        payloads = [json.loads(e['payload']) for e in s.state.events(1)
                    if e['kind'] == 'repair']
        self.assertEqual(payloads[0]['cause'], 'merge-conflict')
        self.assertEqual(payloads[0]['paths'],
                         ['docs/plan.md', 'agent_pool/state.py'])

    def test_merge_conflict_repair_falls_back_to_worktree_unmerged_paths(self):
        s=self.supervisor
        s.dispatch(self.github.items)
        s.reconcile_workers(self.github.items)
        self.github.includes_main=lambda head, base: False
        out = ("Auto-merging docs/plan.md\n"
               "Automatic merge failed; fix conflicts and then commit the result.\n")
        def run_git(cwd, *args):
            if args and args[0] == 'merge':
                raise subprocess.CalledProcessError(1, 'merge', out, 'error: conflict')
            if args[:3] == ('diff', '--name-only', '--diff-filter=U'):
                return 'docs/plan.md\nagent_pool/state.py'
            return 'base'
        self.runtime.run_git.side_effect=run_git
        s.integrate(self.github.items)
        self.assertEqual(s.state.job(1)['repairs'],1)
        payloads = [json.loads(e['payload']) for e in s.state.events(1)
                    if e['kind'] == 'repair']
        self.assertEqual(payloads[0]['cause'], 'merge-conflict')
        self.assertEqual(payloads[0]['paths'],
                         ['docs/plan.md', 'agent_pool/state.py'])
        report = s.state.merge_flow(now=time.time() + 60)
        self.assertEqual(report['conflict_paths']['current'],
                         {'docs/plan.md': 1, 'agent_pool/state.py': 1})
        self.assertEqual(report['conflict_load']['current'], 'concentrated')

    def test_modify_delete_conflict_attributes_via_unmerged_paths(self):
        s=self.supervisor
        s.dispatch(self.github.items)
        s.reconcile_workers(self.github.items)
        self.github.includes_main=lambda head, base: False
        out = ("CONFLICT (modify/delete): docs/old.md deleted in HEAD and "
               "modified in abc1234.  Version abc1234 of docs/old.md left "
               "in tree.\nAutomatic merge failed; fix conflicts and then "
               "commit the result.\n")
        def run_git(cwd, *args):
            if args and args[0] == 'merge':
                raise subprocess.CalledProcessError(1, 'merge', out, '')
            if args[:3] == ('diff', '--name-only', '--diff-filter=U'):
                return 'docs/old.md'
            return 'base'
        self.runtime.run_git.side_effect=run_git
        s.integrate(self.github.items)
        payloads = [json.loads(e['payload']) for e in s.state.events(1)
                    if e['kind'] == 'repair']
        self.assertEqual(payloads[0]['cause'], 'merge-conflict')
        self.assertEqual(payloads[0]['paths'], ['docs/old.md'])

    def test_push_failure_after_clean_merge_is_publish_error(self):
        s=self.supervisor
        s.dispatch(self.github.items)
        s.reconcile_workers(self.github.items)
        self.github.includes_main=lambda head, base: False
        def run_git(cwd, *args):
            if args and args[0] == 'push':
                raise subprocess.CalledProcessError(1, 'push', '', 'remote rejected')
            return 'base'
        self.runtime.run_git.side_effect=run_git
        s.integrate(self.github.items)
        events = self.attributed_events(1)
        self.assertIn(('repair','publish-error'), events)
        self.assertNotIn(('repair','merge-conflict'), events)

    def test_merge_conflict_repair_with_unrecoverable_paths_records_pathless(self):
        s=self.supervisor
        s.dispatch(self.github.items)
        s.reconcile_workers(self.github.items)
        self.github.includes_main=lambda head, base: False
        def run_git(cwd, *args):
            if args and args[0] == 'merge':
                raise subprocess.CalledProcessError(1, 'merge', None, None)
            return ''
        self.runtime.run_git.side_effect=run_git
        s.integrate(self.github.items)
        payloads = [json.loads(e['payload']) for e in s.state.events(1)
                    if e['kind'] == 'repair']
        self.assertEqual(payloads[0], {'cause': 'merge-conflict', 'paths': [],
                                       'pathless': True})
        report = s.state.merge_flow(now=time.time() + 60)
        self.assertEqual(report['conflict_paths']['current'], {'pathless': 1})
        self.assertEqual(report['conflict_load']['current'], 'unattributed')

    def test_clean_cause_repair_records_no_paths(self):
        s=self.supervisor
        s.dispatch(self.github.items)
        self.runtime.inspect_result.side_effect=ValueError('unclean result')
        s.reconcile_workers(self.github.items)
        payloads = [json.loads(e['payload']) for e in s.state.events(1)
                    if e['kind'] == 'repair']
        self.assertEqual(payloads[0], {'cause': 'publish-error'})

    def test_ci_failure_repair_persists_failed_check_names(self):
        s=self.supervisor
        s.dispatch(self.github.items)
        s.reconcile_workers(self.github.items)
        self.github.checks={'test':'failure','verify':'success'}
        s.integrate(self.github.items)
        payloads = [json.loads(e['payload']) for e in s.state.events(1)
                    if e['kind'] == 'repair']
        self.assertEqual(payloads[0]['cause'], 'ci-failure')
        self.assertEqual(payloads[0]['checks'], ['test'])

    def test_publish_error_repair_records_publish_cause(self):
        s=self.supervisor
        s.dispatch(self.github.items)
        self.runtime.inspect_result.side_effect=ValueError('unclean result')
        s.reconcile_workers(self.github.items)
        self.assertIn(('repair','publish-error'), self.attributed_events(1))

    def test_blocked_worker_retry_records_worker_failure_cause(self):
        s=self.supervisor
        s.dispatch(self.github.items)
        self.runtime.poll.return_value={'exit_code':1}
        s.reconcile_workers(self.github.items)
        self.assertEqual(s.state.job(1)['status'],'blocked')
        self.runtime.pool_root=Path(self.config['pool_root'])
        s.state.set('recovery:1', {'branch':'codex/issue-1-1',
                                   'clone':s.state.job(1)['clone'],
                                   'work':False,'phase':'captured'})
        s.state.set('retry:1', True)
        s.retries(self.github.items)
        self.assertEqual(s.state.job(1)['status'],'working')
        self.assertIn(('redispatch','worker-failure'), self.attributed_events(1))

    def test_quota_requeue_records_quota_requeue_cause(self):
        s=self.supervisor
        s.dispatch(self.github.items)
        self.runtime.poll.return_value={'exit_code':1}
        log = Path(self.tmp.name)/'quota.log'
        log.write_text('Reached free model rate limit')
        record = s.state.get('process:1')
        record['log']=str(log)
        s.state.set('process:1', record)
        s.reconcile_workers(self.github.items)
        self.assertEqual(s.state.job(1)['status'],'blocked')
        self.assertEqual(s.state.get('retry:1'),'quota-requeue')
        s.admission.reset('swe-2-high')
        self.runtime.pool_root=Path(self.config['pool_root'])
        s.state.set('recovery:1', {'branch':'codex/issue-1-1',
                                   'clone':s.state.job(1)['clone'],
                                   'work':False,'phase':'captured'})
        s.retries(self.github.items)
        self.assertIn(('redispatch','quota-requeue'), self.attributed_events(1))

    def test_quota_requeue_does_not_dispatch_before_delay(self):
        s=self.supervisor
        s.dispatch(self.github.items)
        self.kill_worker('Reached free model rate limit')
        self.assertEqual(s.state.job(1)['status'],'blocked')
        self.assertEqual(s.state.get('retry:1'),'quota-requeue')
        rec = s.state.get('recovery:1')
        requeue = rec['requeue']
        self.assertEqual(requeue['source'],'default')
        self.assertEqual(requeue['seconds'],s.admission.quota_requeue_delay)
        self.assertGreaterEqual(requeue['not_before'],
                                time.time() + requeue['seconds'] - 5)
        self.runtime.pool_root=Path(self.config['pool_root'])
        rec = self.recoverable()
        s.admission.reset('swe-2-high')
        s.retries(self.github.items)
        # The armed retry holds until the recorded delay has elapsed.
        self.assertEqual(s.state.job(1)['status'],'blocked')
        self.assertNotIn(('redispatch','quota-requeue'), self.attributed_events(1))
        rec['requeue']['not_before'] = time.time() - 1
        s.state.set('recovery:1', rec)
        s.retries(self.github.items)
        self.assertEqual(s.state.job(1)['status'],'working')
        redispatches = [json.loads(e['payload']) for e in s.state.events(1)
                        if e['kind'] == 'redispatch']
        self.assertEqual(redispatches[0]['cause'],'quota-requeue')
        self.assertEqual(redispatches[0]['delay'],
                         {'source':'default','seconds':s.admission.quota_requeue_delay})

    def test_quota_requeue_retry_after_from_receipt(self):
        s=self.supervisor
        s.dispatch(self.github.items)
        self.kill_worker('Rate limit exceeded. Retry after 120 seconds.')
        self.assertEqual(s.state.get('retry:1'),'quota-requeue')
        rec = s.state.get('recovery:1')
        requeue = rec['requeue']
        self.assertEqual(requeue['source'],'retry-after')
        self.assertEqual(requeue['seconds'],120)
        self.assertAlmostEqual(requeue['not_before'],time.time() + 120,delta=5)
        self.runtime.pool_root=Path(self.config['pool_root'])
        rec = self.recoverable()
        s.admission.reset('swe-2-high')
        s.retries(self.github.items)
        self.assertEqual(s.state.job(1)['status'],'blocked')
        rec['requeue']['not_before'] = time.time() - 1
        s.state.set('recovery:1', rec)
        s.retries(self.github.items)
        self.assertEqual(s.state.job(1)['status'],'working')
        redispatches = [json.loads(e['payload']) for e in s.state.events(1)
                        if e['kind'] == 'redispatch']
        self.assertEqual(redispatches[0]['delay'],
                         {'source':'retry-after','seconds':120})

    def test_quota_requeue_hint_is_bounded_by_the_cooldown_ceiling(self):
        s=self.supervisor
        s.dispatch(self.github.items)
        self.kill_worker('Rate limit exceeded. Retry-After: 999999')
        rec = s.state.get('recovery:1')
        self.assertEqual(rec['requeue']['source'],'retry-after')
        self.assertEqual(rec['requeue']['seconds'], s.admission.hint_ceiling)

    def test_bare_retry_prose_never_arms_a_quota_requeue(self):
        """#1366: an agent's own retry prose is not a provider quota event."""
        s=self.supervisor
        s.dispatch(self.github.items)
        self.kill_worker('test failed: flaky endpoint, will retry after 30 seconds')
        self.assertFalse(s.state.get('retry:1'))
        self.assertEqual(s.admission.summary()['groups']['swe-2-high']['mode'],'normal')

    def test_quota_requeue_budget_exhaustion_still_parks(self):
        s=self.supervisor
        s.admission.max_quota_requeues = 1
        s.dispatch(self.github.items)
        self.kill_worker('Reached free model rate limit')
        self.runtime.pool_root=Path(self.config['pool_root'])
        rec = self.recoverable()
        s.admission.reset('swe-2-high')
        rec['requeue']['not_before'] = time.time() - 1
        s.state.set('recovery:1', rec)
        s.retries(self.github.items)
        self.assertEqual(s.state.job(1)['status'],'working')
        # The next quota death spends no further requeue: the bound parks it.
        self.kill_worker('Reached free model rate limit')
        self.assertEqual(s.state.job(1)['status'],'blocked')
        self.assertFalse(s.state.get('retry:1'))
        log_text = (Path(self.config['state_root'])/'supervisor.log').read_text()
        self.assertIn('quota requeue budget exhausted (1)',log_text)
        s.retries(self.github.items)
        self.assertEqual(s.state.job(1)['status'],'blocked')

    def spend_episode_once(self, s):
        """Dispatch, spend the 1-deep episode budget, and re-dispatch."""
        s.dispatch(self.github.items)
        self.runtime.pool_root=Path(self.config['pool_root'])
        self.kill_worker('Reached free model rate limit')
        rec = self.recoverable()
        s.admission.reset('swe-2-high')
        rec['requeue']['not_before'] = time.time() - 1
        s.state.set('recovery:1', rec)
        s.retries(self.github.items)
        self.assertEqual(s.state.job(1)['status'],'working')

    def test_quota_episode_rearms_after_congestion_quiets(self):
        s=self.supervisor
        s.admission.max_quota_requeues = 1
        self.spend_episode_once(s)
        # The next quota death spends the episode budget; the covering
        # group's live congestion episode holds a quiet window open instead
        # of parking the issue forever.
        self.kill_worker('Reached free model rate limit')
        self.assertEqual(s.state.job(1)['status'],'blocked')
        self.assertFalse(s.state.get('retry:1'))
        rec = s.state.get('recovery:1')
        self.assertEqual(rec['quota_episode']['groups'],['swe-2-high'])
        self.assertEqual(rec['quota_requeues'],1)
        log_text = (Path(self.config['state_root'])/'supervisor.log').read_text()
        self.assertIn('holding for congestion quiet on swe-2-high',log_text)
        # While the group still probes, the hold does not re-arm.
        s.retries(self.github.items)
        self.assertEqual(s.state.job(1)['status'],'blocked')
        # Once the group reports a quiet window, the hold re-arms one retry
        # with a fresh episode budget.
        s.admission.reset('swe-2-high')
        s.admission.quiet = 0
        self.recoverable()
        s.retries(self.github.items)
        self.assertEqual(s.state.job(1)['status'],'working')
        rec = s.state.get('recovery:1')
        self.assertEqual(rec['quota_requeues'],0)
        self.assertEqual(rec['quota_requeue_resets'],1)
        self.assertFalse(rec.get('quota_episode'))
        redispatches = [json.loads(e['payload']) for e in s.state.events(1)
                        if e['kind'] == 'redispatch']
        self.assertEqual(redispatches[0]['cause'],'quota-requeue')
        self.assertEqual(redispatches[0]['delay'],
                         {'source':'quiet-window','seconds':0})
        self.assertEqual(redispatches[1]['delay']['source'],'default')
        # The fresh episode budget re-arms normally on the next quota death.
        self.kill_worker('Reached free model rate limit')
        self.assertEqual(s.state.get('retry:1'),'quota-requeue')

    def test_quota_episode_reset_bound_leaves_permanent_park(self):
        s=self.supervisor
        s.admission.max_quota_requeues = 1
        s.admission.max_quota_requeue_resets = 1
        s.admission.quiet = 0
        self.spend_episode_once(s)
        # The first spent episode quiets and re-arms through its one reset.
        self.kill_worker('Reached free model rate limit')
        s.admission.reset('swe-2-high')
        self.recoverable()
        s.retries(self.github.items)
        self.assertEqual(s.state.job(1)['status'],'working')
        # The fresh episode budget arms and spends once more; the next spent
        # episode exceeds the reset bound: permanent park, exhaustion logged.
        self.kill_worker('Reached free model rate limit')
        self.assertEqual(s.state.get('retry:1'),'quota-requeue')
        rec = self.recoverable()
        s.admission.reset('swe-2-high')
        rec['requeue']['not_before'] = time.time() - 1
        s.state.set('recovery:1', rec)
        s.retries(self.github.items)
        self.assertEqual(s.state.job(1)['status'],'working')
        self.kill_worker('Reached free model rate limit')
        self.assertEqual(s.state.job(1)['status'],'blocked')
        self.assertFalse(s.state.get('retry:1'))
        log_text = (Path(self.config['state_root'])/'supervisor.log').read_text()
        self.assertIn('quota requeue budget exhausted (1) after '
                      '1 quiet-window resets',log_text)
        s.admission.reset('swe-2-high')
        self.recoverable()
        s.retries(self.github.items)
        self.assertEqual(s.state.job(1)['status'],'blocked')

    def test_quota_exhaustion_without_live_episode_stays_parked(self):
        s=self.supervisor
        s.admission.max_quota_requeues = 1
        self.spend_episode_once(s)
        # An auth/credit stop on the covering group is not a congestion
        # episode: no quiet window applies to the spent budget.
        s.admission.finish('external', {'model':'swe-2-high'}, 'auth')
        self.assertEqual(s.admission.summary()['groups']['swe-2-high']['mode'],
                         'blocked')
        self.kill_worker('Reached free model rate limit')
        self.assertEqual(s.state.job(1)['status'],'blocked')
        self.assertFalse(s.state.get('retry:1'))
        rec = s.state.get('recovery:1')
        self.assertFalse(rec.get('quota_episode'))
        log_text = (Path(self.config['state_root'])/'supervisor.log').read_text()
        self.assertIn('quota requeue budget exhausted (1); leaving blocked',
                      log_text)
        # The manual operator retry path still un-parks the job.
        s.admission.reset('swe-2-high')
        self.recoverable()
        s.state.set('retry:1', True)
        s.retries(self.github.items)
        self.assertEqual(s.state.job(1)['status'],'working')

    def test_quota_episode_state_survives_supervisor_restart(self):
        s=self.supervisor
        s.admission.max_quota_requeues = 1
        s.dispatch(self.github.items)
        self.runtime.pool_root=Path(self.config['pool_root'])
        self.kill_worker('Reached free model rate limit')
        # The armed retry flag and the requeue schedule are durable state.
        restarted=Supervisor(self.config)
        restarted.github=self.github
        restarted.runtime=self.runtime
        try:
            self.assertEqual(restarted.state.get('retry:1'),'quota-requeue')
            rec = restarted.state.get('recovery:1')
            self.assertEqual(rec['requeue']['source'],'default')
            self.assertEqual(rec['quota_requeues'],1)
        finally:
            restarted.state.close()
        rec = self.recoverable()
        s.admission.reset('swe-2-high')
        rec['requeue']['not_before'] = time.time() - 1
        s.state.set('recovery:1', rec)
        s.retries(self.github.items)
        self.assertEqual(s.state.job(1)['status'],'working')
        # The spent-episode hold is durable too: a restarted supervisor sees
        # the same pending quiet window and re-arms it once it quiets.
        self.kill_worker('Reached free model rate limit')
        rec = s.state.get('recovery:1')
        self.assertEqual(rec['quota_episode']['groups'],['swe-2-high'])
        restarted=Supervisor(self.config)
        restarted.github=self.github
        restarted.runtime=self.runtime
        try:
            rec = restarted.state.get('recovery:1')
            self.assertEqual(rec['quota_episode']['groups'],['swe-2-high'])
            self.assertEqual(rec['quota_requeues'],1)
            self.assertEqual(rec['quota_requeue_resets'],0)
            self.assertFalse(restarted.state.get('retry:1'))
            restarted.admission.reset('swe-2-high')
            restarted.admission.quiet = 0
            rec.update(work=False, phase='captured')
            restarted.state.set('recovery:1', rec)
            restarted.retries(self.github.items)
            self.assertEqual(restarted.state.job(1)['status'],'working')
            redispatches = [json.loads(e['payload'])
                            for e in restarted.state.events(1)
                            if e['kind'] == 'redispatch']
            self.assertEqual(redispatches[0]['delay']['source'],'quiet-window')
        finally:
            restarted.state.close()

    def test_non_quota_receipt_arms_no_delayed_retry(self):
        s=self.supervisor
        s.dispatch(self.github.items)
        self.kill_worker('process exited unexpectedly')
        self.assertEqual(s.state.job(1)['status'],'blocked')
        self.assertFalse(s.state.get('retry:1'))
        rec = s.state.get('recovery:1') or {}
        self.assertFalse(rec.get('requeue'))

    def test_missing_checks_do_not_merge_or_repair(self):
        s=self.supervisor
        s.dispatch(self.github.items)
        s.reconcile_workers(self.github.items)
        self.github.checks={}
        s.integrate(self.github.items)
        self.assertEqual(s.state.job(1)['repairs'],0)
        self.assertFalse(self.github.pr(1)['merged'])

    def test_launch_intent_recovers_orphan(self):
        s=self.supervisor
        s.dispatch(self.github.items)
        record=s.state.get('process:1')
        s.state.set('process:1',None)
        self.runtime.recover.return_value=[record]
        s.recover_processes()
        self.assertEqual(s.state.get('process:1'),record)

    def test_disabled_planning_never_publishes_and_existing_work_still_runs(self):
        s = self.supervisor
        s.config['planning'] = {'enabled': False}
        candidate = dict(key='new-root', title='Goal expansion', scope='Expand scope',
                         acceptance='New capability', tests='simulation',
                         dependencies=[], priority=1, milestone='future',
                         group='core', area='engineering')
        proposal = {'issues': [candidate], 'dispositions': []}
        output = Path(self.tmp.name) / 'disabled-proposal.json'
        output.write_text(json.dumps(proposal))
        s.github.create_issue = Mock(return_value=99)
        for scenario in ('periodic', 'forced', 'pending', 'completed', 'running'):
            with self.subTest(scenario=scenario):
                self.runtime.reset_mock()
                s.state.set('last_plan', 0)
                s.state.set('plan:requested', scenario == 'forced')
                s.state.set('pending_proposal', proposal if scenario == 'pending' else None)
                current = scenario in ('completed', 'running')
                s.state.set('planner', {'process': {'key': 'planner'},
                                       'output': str(output)} if current else None)
                self.runtime.poll.return_value = None if scenario == 'running' else {'exit_code': 0}
                if current:
                    self.assertTrue(s.admission.reserve('planner', 'swe-2-high', 'coordinator', 3))
                s.planner(self.github.items, [])
                if scenario == 'running':
                    self.runtime.poll.return_value = {'exit_code': 0}
                    s.planner(self.github.items, [])
                s.github.create_issue.assert_not_called()
                self.runtime.spawn.assert_not_called()
                self.assertIsNone(s.state.get('pending_proposal'))
                self.assertIsNone(s.state.get('planner'))
                self.assertFalse(s.state.get('plan:requested'))
                self.assertEqual(s.state.db.execute(
                    "SELECT COUNT(*) FROM admission_leases WHERE owner='planner'").fetchone()[0], 0)
        s.dispatch(self.github.items)
        self.assertEqual(s.state.job(1)['status'], 'working')

    def test_rejected_proposal_feeds_back_to_next_planner(self):
        s=self.supervisor
        out = Path(self.tmp.name)/'proposal.json'
        out.write_text(json.dumps({'issues':[],'dispositions':[{'id':'x','disposition':'resolved','reason':'r'}]}))
        s.state.set('planner', {'process':{'key':'p'},'output':str(out)})
        s.planner([], [])
        self.assertIsNone(s.state.get('pending_proposal'))
        self.assertIn('disposition', s.state.get('planner_feedback'))
        s.state.set('last_plan', 0)
        s.planner([], [])
        self.assertIn('Invalid disposition fields', self.runtime.spawn.call_args[0][2])

    def test_planner_publishes_root_when_ready_labels_are_dependency_blocked(self):
        s = self.supervisor
        issues = [issue(1)] + [issue(n, dependencies=(1,)) for n in range(2, 22)]
        issues[0]['labels'] = [{'name': 'agent:blocked'}]
        candidate = dict(key='new-root', title='Independent repair', scope='Repair dispatch',
                         acceptance='Dispatch resumes', tests='supervisor test',
                         dependencies=[], priority=1, milestone='recovery',
                         group='agent_pool', area='delivery-platform')
        s.state.set('pending_proposal', {'issues': [candidate], 'dispositions': []})
        s.github.create_issue = Mock(return_value=99)
        s.planner(issues, [])
        s.github.create_issue.assert_called_once()
        self.assertIsNone(s.state.get('pending_proposal'))

    def test_planner_caps_dispatchable_frontier(self):
        s = self.supervisor
        issues = [issue(n) for n in range(1, 21)]
        candidate = dict(key='new-root', title='Independent repair', scope='Repair dispatch',
                         acceptance='Dispatch resumes', tests='supervisor test',
                         dependencies=[], priority=1, milestone='recovery',
                         group='agent_pool', area='delivery-platform')
        s.state.set('pending_proposal', {'issues': [candidate], 'dispositions': []})
        s.github.create_issue = Mock(return_value=99)
        s.planner(issues, [])
        s.github.create_issue.assert_not_called()

    def test_unowned_running_invocation_pauses(self):
        self.runtime.recover.return_value=[{'invocation':'orphan','key':'unknown'}]
        self.runtime.poll.return_value=None
        self.supervisor.recover_processes()
        self.assertTrue(self.supervisor.state.paused())
        self.runtime.terminate.assert_called_once()
