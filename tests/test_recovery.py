import shutil
import tempfile
import time
import unittest
from pathlib import Path

from agent_pool import planning
from agent_pool.runtime import Runtime
from agent_pool.supervisor import Supervisor


def issue(number=1, group='core', dependencies=()):
    item = dict(key=f'issue-{number}', title=f'Task {number}', scope='s', acceptance='a',
                tests='t', dependencies=list(dependencies), priority=2,
                milestone='m', group=group)
    return dict(number=number, title=item['title'], body=planning.body(item),
                state='OPEN', labels=[{'name': 'agent:ready'}])


class FakeGitHub:
    def __init__(self, items):
        self.items = items


class FakeRuntime(Runtime):
    """Real git/clone behavior; spawn only records the launch."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.spawned = []

    def spawn(self, key, cwd, prompt, resume_session=None):
        self.spawned.append({'key': key, 'cwd': str(cwd), 'resume': resume_session})
        return {'key': key, 'cwd': str(cwd), 'pid': 999999999, 'identity': 'fake',
                'receipt': str(self.state_root / key / 'receipt.json'),
                'invocation': str(self.state_root / key), 'log': '/nonexistent',
                'started_at': time.time()}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.git(self.source, 'init', '-b', 'main')
        self.git(self.source, 'config', 'user.email', 'test@example.com')
        self.git(self.source, 'config', 'user.name', 'Test')
        (self.source / 'README').write_text('vision')
        self.git(self.source, 'add', 'README')
        self.git(self.source, 'commit', '-m', 'initial')
        self.config = dict(state_root=str(self.root / 'state'),
                           pool_root=str(self.root / 'pool'),
                           repository=str(self.source), timeout_seconds=7200,
                           required_checks=['test'], poll_seconds=60)
        self.gh = FakeGitHub([issue(1)])
        self.sup = Supervisor(self.config)
        self.sup.github = self.gh
        self.runtime = FakeRuntime(self.root / 'pool', self.root / 'state', str(self.source))
        self.sup.runtime = self.runtime

    def tearDown(self):
        self.sup.state.close()
        self.tmp.cleanup()

    def git(self, cwd, *args):
        return Runtime.run_git(cwd, *args)

    def start_job(self, number=1):
        """Dispatch an issue and return (job, clone) with committer identity set."""
        self.sup.dispatch([i for i in self.gh.items if i['number'] == number])
        job = self.sup.state.job(number)
        self.assertEqual(job['status'], 'working')
        clone = Path(job['clone'])
        self.git(clone, 'config', 'user.email', 'test@example.com')
        self.git(clone, 'config', 'user.name', 'Test')
        return job, clone

    def commit_file(self, clone, name, content='work'):
        (Path(clone) / name).write_text(content)
        self.git(clone, 'add', name)
        self.git(clone, 'commit', '-m', 'wip')
        return self.git(clone, 'rev-parse', 'HEAD')

    def occupy(self, number, worker, clone_name=None):
        """Register a fake live job holding a worker and optionally a clone."""
        self.sup.state.reserve(number, worker, 'g' + str(number))
        if clone_name:
            self.sup.state.update_job(number, clone=str(self.runtime.pool_root / clone_name))

    def test_retry_restores_branch_switched_clone(self):
        """The reported failure: clone reused onto another branch, then retried."""
        job, clone = self.start_job(1)
        head = self.commit_file(clone, 'feature.rs')
        self.sup.block(self.sup.state.job(1), 'stopped')
        self.git(clone, 'switch', '-c', 'codex/issue-9-1', 'origin/main')
        self.sup.state.set('retry:1', True)
        self.sup.retries(self.gh.items)
        job = self.sup.state.job(1)
        self.assertEqual(job['status'], 'working')
        self.assertEqual(self.git(clone, 'branch', '--show-current'), 'codex/issue-1-1')
        self.assertEqual(self.git(clone, 'rev-parse', 'HEAD'), head)
        self.assertEqual(len(self.runtime.spawned), 2)
        self.assertEqual(Path(self.runtime.spawned[-1]['cwd']), clone)

    def test_retry_recovers_into_free_clone_when_original_busy(self):
        job, clone = self.start_job(1)
        head = self.commit_file(clone, 'feature.rs')
        self.sup.block(self.sup.state.job(1), 'stopped')
        self.gh.items.append(issue(2, group='other'))
        self.start_job(2)
        self.assertEqual(self.sup.state.job(2)['clone'], str(clone))
        self.sup.state.set('retry:1', True)
        self.sup.retries(self.gh.items)
        job = self.sup.state.job(1)
        self.assertEqual(job['status'], 'working')
        self.assertNotEqual(job['clone'], str(clone))
        recovered = Path(job['clone'])
        self.assertEqual(self.git(recovered, 'branch', '--show-current'), 'codex/issue-1-1')
        self.assertEqual(self.git(recovered, 'rev-parse', 'HEAD'), head)
        self.assertEqual((recovered / 'feature.rs').read_text(), 'work')

    def test_retry_preserves_uncommitted_work(self):
        job, clone = self.start_job(1)
        (clone / 'wip.rs').write_text('half done')
        self.sup.block(self.sup.state.job(1), 'stopped')
        head = self.git(clone, 'rev-parse', 'HEAD')
        self.git(clone, 'switch', '-c', 'codex/issue-9-1', 'origin/main')
        self.sup.state.set('retry:1', True)
        self.sup.retries(self.gh.items)
        self.assertEqual(self.sup.state.job(1)['status'], 'working')
        self.assertEqual(self.git(clone, 'rev-parse', 'HEAD'), head)
        self.assertEqual((clone / 'wip.rs').read_text(), 'half done')

    def test_retry_fresh_start_when_no_work_exists(self):
        job, clone = self.start_job(1)
        self.sup.block(self.sup.state.job(1), 'agent failed before any change')
        rec = self.sup.state.get('recovery:1')
        self.assertFalse(rec['work'])
        self.sup.state.set('retry:1', True)
        self.sup.retries(self.gh.items)
        self.assertEqual(self.sup.state.job(1)['status'], 'working')
        self.assertEqual(self.git(clone, 'branch', '--show-current'), 'codex/issue-1-1')
        self.assertFalse(self.git(clone, 'diff', '--name-only', 'origin/main...HEAD'))

    def test_uncertain_recovery_never_starts_fresh(self):
        job, clone = self.start_job(1)
        self.commit_file(clone, 'feature.rs')
        self.sup.block(self.sup.state.job(1), 'stopped')
        rec = self.sup.state.get('recovery:1')
        rec.update(work=None, detail='survey damaged', phase='captured')
        self.sup.state.set('recovery:1', rec)
        self.sup.state.set('retry:1', True)
        self.sup.retries(self.gh.items)
        job = self.sup.state.job(1)
        self.assertEqual(job['status'], 'blocked')
        self.assertIn('uncertain', job['error'].lower())
        self.assertEqual(len(self.runtime.spawned), 1)

    def test_failed_checkout_is_not_treated_as_no_work(self):
        job, clone = self.start_job(1)
        self.commit_file(clone, 'feature.rs')
        self.sup.block(self.sup.state.job(1), 'stopped')
        rec = self.sup.state.get('recovery:1')
        rec.update(head='0' * 40, sources=[], phase='captured')
        self.sup.state.set('recovery:1', rec)
        self.git(clone, 'switch', '-c', 'codex/issue-9-1', 'origin/main')
        self.git(clone, 'branch', '-D', 'codex/issue-1-1')
        self.sup.state.set('retry:1', True)
        self.sup.retries(self.gh.items)
        job = self.sup.state.job(1)
        self.assertEqual(job['status'], 'blocked')
        self.assertIn('Recovery failed', job['error'])
        rec = self.sup.state.get('recovery:1')
        self.assertTrue(rec['work'])
        self.assertEqual(len(self.runtime.spawned), 1)

    def test_retry_waits_for_free_clone(self):
        job, clone = self.start_job(1)
        self.commit_file(clone, 'feature.rs')
        self.sup.block(self.sup.state.job(1), 'stopped')
        self.occupy(10, 'worker-02', clone_name='worker-01')  # original clone leased
        self.occupy(11, 'worker-03', clone_name='worker-03')
        self.occupy(12, 'worker-04', clone_name='worker-04')
        self.occupy(13, 'worker-05', clone_name='worker-05')
        self.sup.state.set('retry:1', True)
        self.sup.retries(self.gh.items)
        job = self.sup.state.job(1)
        self.assertEqual(job['status'], 'blocked')
        self.assertEqual(self.sup.state.get('recovery:1')['phase'], 'waiting')
        self.assertTrue(self.sup.state.get('retry:1'))
        self.assertEqual(len(self.runtime.spawned), 1)

    def test_restart_completes_partial_recovery_once(self):
        job, clone = self.start_job(1)
        head = self.commit_file(clone, 'feature.rs')
        self.sup.block(self.sup.state.job(1), 'stopped')
        self.git(clone, 'switch', '-c', 'codex/issue-9-1', 'origin/main')
        self.sup.state.set('retry:1', True)
        rec = self.sup.state.get('recovery:1')
        rec.update(phase='leased', target_clone=str(clone), worker='worker-01')
        self.sup.state.set('recovery:1', rec)
        self.sup.state.close()
        self.sup = Supervisor(self.config)
        self.sup.github = self.gh
        self.sup.runtime = self.runtime
        self.sup.retries(self.gh.items)
        self.sup.retries(self.gh.items)
        job = self.sup.state.job(1)
        self.assertEqual(job['status'], 'working')
        self.assertEqual(self.git(clone, 'rev-parse', 'HEAD'), head)
        self.assertEqual(len(self.runtime.spawned), 2)

    def test_concurrent_retries_get_distinct_clones(self):
        job1, clone1 = self.start_job(1)
        head1 = self.commit_file(clone1, 'one.rs')
        self.sup.block(self.sup.state.job(1), 'stopped')
        self.gh.items.append(issue(2, group='other'))
        job2, clone2 = self.start_job(2)
        self.assertEqual(clone2, clone1)
        head2 = self.commit_file(clone2, 'two.rs')
        self.sup.block(self.sup.state.job(2), 'stopped')
        self.sup.state.set('retry:1', True)
        self.sup.state.set('retry:2', True)
        self.sup.retries(self.gh.items)
        job1, job2 = self.sup.state.job(1), self.sup.state.job(2)
        self.assertEqual(job1['status'], 'working')
        self.assertEqual(job2['status'], 'working')
        self.assertNotEqual(job1['clone'], job2['clone'])
        self.assertEqual(self.git(job1['clone'], 'rev-parse', 'HEAD'), head1)
        self.assertEqual(self.git(job2['clone'], 'rev-parse', 'HEAD'), head2)

    def test_missing_clone_recovers_from_remote_ref(self):
        job, clone = self.start_job(1)
        head = self.commit_file(clone, 'feature.rs')
        self.git(clone, 'push', 'origin', 'codex/issue-1-1')
        self.sup.block(self.sup.state.job(1), 'stopped')
        shutil.rmtree(clone)
        self.sup.state.set('retry:1', True)
        self.sup.retries(self.gh.items)
        job = self.sup.state.job(1)
        self.assertEqual(job['status'], 'working')
        recovered = Path(job['clone'])
        self.assertTrue((recovered / '.git').exists())
        self.assertEqual(self.git(recovered, 'branch', '--show-current'), 'codex/issue-1-1')
        self.assertEqual(self.git(recovered, 'rev-parse', 'HEAD'), head)

    def test_recovery_record_survives_block_and_done_clears_it(self):
        job, clone = self.start_job(1)
        self.commit_file(clone, 'feature.rs')
        self.sup.block(self.sup.state.job(1), 'stopped')
        rec = self.sup.state.get('recovery:1')
        self.assertTrue(rec['work'])
        self.assertEqual(rec['branch'], 'codex/issue-1-1')
        self.assertEqual(rec['head'], self.git(clone, 'rev-parse', 'HEAD'))
        self.sup.state.complete(1)
        self.assertIsNone(self.sup.state.get('recovery:1'))


if __name__ == '__main__':
    unittest.main()
