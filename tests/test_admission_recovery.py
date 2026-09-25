"""Scoped quota recovery through the real supervisor paths.

Regression coverage for the 2026-09-19 audit: a rate-limited invocation must
cool down only its own quota group, requeue once per failed invocation, wait
when every group is saturated, and recover through a single probe — never a
global pause, never a retry wave, and never past an admission lease.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path

from agent_pool import planning
from agent_pool.runtime import Runtime
from agent_pool.supervisor import Supervisor


def issue(number=1):
    item = dict(key=f'issue-{number}', title='T', scope='s', acceptance='a', tests='t',
                dependencies=[], priority=2, milestone='m', group='g', area='control-runtime')
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
        self.log_path = '/nonexistent'

    def spawn(self, key, cwd, prompt, resume_session=None, timeout=None, model='swe-2-high'):
        self.spawned.append({'key': key, 'model': model})
        return {'key': key, 'cwd': str(cwd), 'pid': 1, 'identity': 'x',
                'invocation': str(self.state_root / key), 'log': self.log_path,
                'started_at': time.time()}


class ScopedRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.src = root / 'src'
        self.src.mkdir()
        Runtime.run_git(self.src, 'init', '-b', 'main')
        Runtime.run_git(self.src, 'config', 'user.email', 't@t')
        Runtime.run_git(self.src, 'config', 'user.name', 't')
        (self.src / 'f').write_text('x')
        Runtime.run_git(self.src, 'add', 'f')
        Runtime.run_git(self.src, 'commit', '-m', 'i')
        self.now = [time.time()]
        self.log = root / 'fail.log'
        self.log.write_text('Reached free model rate limit')
        config = dict(state_root=str(root / 'state'), pool_root=str(root / 'pool'),
                      repository=str(self.src), timeout_seconds=60, required_checks=['t'],
                      poll_seconds=60, models=['swe-2-high', 'opencode/union-alpha'],
                      model_caps={'swe-2-high': 2},
                      scheduler={'cooldown_seconds': 30, 'quiet_seconds': 60})
        self.sup = Supervisor(config)
        self.sup.admission.clock = lambda: self.now[0]
        self.sup.admission.jitter = lambda: 0
        self.sup.github = FakeGitHub([issue(1), issue(2)])
        self.sup.runtime = FakeRuntime(root / 'pool', root / 'state', str(self.src))
        self.sup.runtime.log_path = str(self.log)
        self.sup.runtime.poll = lambda meta: (
            {'exit_code': 1} if meta['key'].startswith('issue-2') else None)

    def tearDown(self):
        self.sup.state.close()
        self.tmp.cleanup()

    def test_rate_limit_scopes_group_requeues_once_and_probes(self):
        sup = self.sup
        # Dispatch: issue 1 on worker-01 (union, slot 1%2), issue 2 on worker-02 (swe).
        sup.dispatch(sup.github.items)
        self.assertEqual(sup.state.job(1)['status'], 'working')
        self.assertEqual(sup.state.job(2)['status'], 'working')
        self.assertEqual(len(sup.runtime.spawned), 2)

        # The swe job dies to a rate limit: blocked + requeued once, swe group
        # probes, the union job is untouched, and the pool does not pause.
        sup.reconcile_workers(sup.github.items)
        self.assertEqual(sup.state.job(2)['status'], 'blocked')
        self.assertEqual(sup.state.get('retry:2'), 'quota-requeue')
        self.assertFalse(sup.state.paused())
        self.assertEqual(sup.state.job(1)['status'], 'working')
        self.assertEqual(sup.admission.summary()['groups']['swe-2-high']['mode'], 'probing')

        # During swe's cooldown the retry migrates to a healthy group's worker
        # instead of dying on the throttled model again; work is preserved.
        sup.retries(sup.github.items)
        job2 = sup.state.job(2)
        self.assertEqual(job2['status'], 'working')
        self.assertEqual(job2['attempt'], 2)
        self.assertEqual(len(sup.runtime.spawned), 3)
        self.assertEqual(sup.runtime.spawned[-1]['model'], 'opencode/union-alpha')
        self.assertEqual(sup.admission.summary()['groups']['swe-2-high']['mode'], 'probing')
        attributed = [(e['kind'], json.loads(e['payload']).get('cause'))
                      for e in sup.state.events(2)]
        self.assertIn(('redispatch', 'quota-requeue'), attributed)

        # The migrated job dies to a rate limit too: union goes into its own
        # probing cooldown and the requeue budget decrements once more.
        sup.reconcile_workers(sup.github.items)
        self.assertEqual(sup.state.job(2)['status'], 'blocked')
        self.assertEqual(sup.admission.summary()['groups']['opencode/union-alpha']['mode'], 'probing')

        # Both cooldowns expire; an outside consumer spends each group's single
        # probe slot. Every admission path — including the preserved-clone one —
        # then leaves the retry waiting instead of launching a wave.
        self.now[0] += 31
        self.assertTrue(sup.admission.reserve('probe-swe', 'swe-2-high', 'x', 5))
        self.assertTrue(sup.admission.reserve('probe-union', 'opencode/union-alpha', 'y', 5))
        sup.retries(sup.github.items)
        self.assertEqual(sup.state.job(2)['status'], 'blocked')
        self.assertEqual(len(sup.runtime.spawned), 3)

        # Once swe's probe slot frees, the retry relaunches as that group's
        # one probe rather than as part of a wave.
        sup.admission.release('probe-swe')
        sup.retries(sup.github.items)
        job2 = sup.state.job(2)
        self.assertEqual(job2['status'], 'working')
        self.assertEqual(job2['attempt'], 3)
        self.assertEqual(len(sup.runtime.spawned), 4)
        self.assertEqual(sup.runtime.spawned[-1]['model'], 'swe-2-high')
        self.assertEqual(sup.admission.summary()['groups']['swe-2-high']['mode'], 'probing')

    def test_auth_failure_blocks_group_until_operator_reset(self):
        sup = self.sup
        sup.dispatch(sup.github.items)
        self.log.write_text('authentication failed')
        sup.reconcile_workers(sup.github.items)
        self.assertEqual(sup.state.job(2)['status'], 'blocked')
        self.assertFalse(sup.state.paused())
        groups = sup.admission.summary()['groups']
        self.assertEqual(groups['swe-2-high']['mode'], 'blocked')
        # The retry flag is not armed for authentication: no amount of waiting
        # fixes credentials, and the group stays closed across any delay.
        self.assertFalse(sup.state.get('retry:2'))
        self.now[0] += 86400
        sup.retries(sup.github.items)
        self.assertEqual(sup.state.job(2)['status'], 'blocked')
        sup.admission.reset('swe-2-high')
        sup.state.set('retry:2', True)
        sup.retries(sup.github.items)
        self.assertEqual(sup.state.job(2)['status'], 'working')

    def test_preserved_clone_retry_cannot_bypass_full_group(self):
        """The orig-clone retry path can no longer launch past a full group.

        Pre-fix, a retried job re-leased its original clone without any cap
        check, so a burst of retry flags launched past model_caps in one tick.
        """
        sup = self.sup
        sup.dispatch(sup.github.items)
        job2 = sup.state.job(2)
        # Preserve real work in job2's clone so recovery prefers the original.
        clone = Path(job2['clone'])
        Runtime.run_git(clone, 'config', 'user.email', 't@t')
        Runtime.run_git(clone, 'config', 'user.name', 't')
        (clone / 'work.rs').write_text('w')
        Runtime.run_git(clone, 'add', 'work.rs')
        Runtime.run_git(clone, 'commit', '-m', 'wip')
        # A plain crash — not a quota event — leaves swe's group in normal mode.
        self.log.write_text('process exited unexpectedly')
        sup.reconcile_workers(sup.github.items)
        self.assertEqual(sup.state.job(2)['status'], 'blocked')
        rec = sup.state.get('recovery:2')
        self.assertTrue(rec['work'])
        self.assertEqual(Path(rec['clone']).name, 'worker-02')
        # Saturate swe's group in normal mode, then retry: the job must migrate
        # to a group with capacity rather than ride its preserved clone past the cap.
        self.assertTrue(sup.admission.reserve('fill-1', 'swe-2-high', 'a', 5))
        self.assertTrue(sup.admission.reserve('fill-2', 'swe-2-high', 'b', 5))
        sup.state.set('retry:2', True)
        sup.retries(sup.github.items)
        job2 = sup.state.job(2)
        self.assertEqual(job2['status'], 'working')
        self.assertEqual(Path(job2['clone']).name, 'worker-03')
        self.assertEqual(sup.runtime.spawned[-1]['model'], 'opencode/union-alpha')
        self.assertEqual(len(sup.runtime.spawned), 3)
        # And when every group is saturated, the retry waits instead of launching.
        sup.reconcile_workers(sup.github.items)
        self.assertEqual(sup.state.job(2)['status'], 'blocked')
        for name in ('u1', 'u2', 'u3'):
            self.assertTrue(sup.admission.reserve(name, 'opencode/union-alpha', name, 20))
        sup.state.set('retry:2', True)
        sup.retries(sup.github.items)
        self.assertEqual(sup.state.job(2)['status'], 'blocked')
        self.assertEqual(len(sup.runtime.spawned), 3)


if __name__ == '__main__':
    unittest.main()
