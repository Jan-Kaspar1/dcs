import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from qa_lane import runner, state as qa_state


SHA_A = 'a' * 40
DAY = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


class Result:
    def __init__(self, stdout='', returncode=0):
        self.stdout = stdout
        self.stderr = ''
        self.returncode = returncode


def cfg_for(root):
    cfg = dict(runner.DEFAULT_CONFIG)
    cfg['state_dir'] = str(Path(root) / 'state')
    cfg['src_dir'] = str(Path(root) / 'state' / 'src')
    cfg['egress_required'] = False
    return cfg


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = cfg_for(self.tmp.name)
        Path(self.cfg['state_dir']).mkdir(parents=True)
        self.st = qa_state.State(Path(self.cfg['state_dir']) / 'state.db')

    def tearDown(self):
        self.st.close()
        self.tmp.cleanup()

    def test_dead_runner_marked_interrupted_and_orphans_removed(self):
        self.st.enqueue('qa-1', SHA_A, 1.0, '2026-09-15')
        self.st.begin('qa-1', 999999, 2.0)
        calls = []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            if args[0] == 'ps':
                return Result('abc123 qa-1\ndef456 qa-1\n')
            if args[:2] == ('network', 'ls'):
                return Result('net9 qa-1\n')
            return Result('')

        with patch.object(runner, 'docker', fake_docker), \
                patch.object(runner, '_pid_alive', return_value=False):
            runner.reconcile(self.st, self.cfg, log=lambda m: None)
        rec = self.st.run('qa-1')
        self.assertEqual(rec['status'], 'interrupted')
        self.assertEqual(rec['outcome'], 'interrupted')
        self.assertIn('abc123', str(calls))
        self.assertIn('net9', str(calls))
        self.assertTrue(any(a[:2] == ('network', 'rm') for a in calls))

    def test_live_runner_keeps_its_containers(self):
        self.st.enqueue('qa-1', SHA_A, 1.0, '2026-09-15')
        self.st.begin('qa-1', 4321, 2.0)
        calls = []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            if args[0] == 'ps':
                return Result('abc123 qa-1\n')
            if args[:2] == ('network', 'ls'):
                return Result('net9 qa-1\n')
            return Result('')

        with patch.object(runner, 'docker', fake_docker), \
                patch.object(runner, '_pid_alive', return_value=True):
            runner.reconcile(self.st, self.cfg, log=lambda m: None)
        self.assertEqual(self.st.run('qa-1')['status'], 'running')
        self.assertFalse(any('rm' in a for a in calls))

    def test_unlabeled_run_orphan_removed(self):
        # A container whose run label matches no record at all is reaped.
        calls = []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            if args[0] == 'ps':
                return Result('dead0 qa-999\n')
            if args[:2] == ('network', 'ls'):
                return Result('')
            return Result('')

        with patch.object(runner, 'docker', fake_docker):
            runner.reconcile(self.st, self.cfg, log=lambda m: None)
        self.assertTrue(any(a[0] == 'rm' and 'dead0' in a for a in calls))


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = cfg_for(self.tmp.name)
        Path(self.cfg['state_dir']).mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_queue_cycle_is_quiet(self):
        logs = []
        with patch.object(runner, 'docker', lambda *a, **k: Result('')):
            runner.cycle(self.cfg, log=logs.append)
        self.assertTrue(any('nothing queued' in m for m in logs))

    def test_budget_blocks_new_runs(self):
        self.cfg['max_runs_per_day'] = 1
        st = qa_state.State(Path(self.cfg['state_dir']) / 'state.db')
        st.enqueue('qa-1', SHA_A, 1.0, '2026-09-15')
        st.begin('qa-1', 1, DAY.timestamp())
        st.finish('qa-1', 'passed', SHA_A, '/r.json',
                  DAY.timestamp() + 60)
        st.enqueue('qa-2', 'b' * 40, DAY.timestamp() + 120,
                   '2026-09-15')
        st.close()
        logs = []
        with patch.object(runner, 'docker', lambda *a, **k: Result('')), \
                patch.object(runner, '_utcnow', return_value=DAY), \
                patch.object(runner, 'run') as mock_run:
            runner.cycle(self.cfg, log=logs.append)
        mock_run.assert_not_called()
        self.assertTrue(any('budget' in m for m in logs))

    def test_inconclusive_gets_one_retry(self):
        self.cfg['max_attempts_per_sha'] = 2
        st = qa_state.State(Path(self.cfg['state_dir']) / 'state.db')
        st.enqueue('qa-1', SHA_A, 1.0, '2026-09-15')
        st.begin('qa-1', 1, 1.0)
        st.finish('qa-1', 'inconclusive', None, '/r.json', 2.0)
        st.close()
        with patch.object(runner, 'docker', lambda *a, **k: Result('')), \
                patch.object(runner, '_utcnow', return_value=DAY), \
                patch.object(runner, 'run') as mock_run:
            runner.cycle(self.cfg, log=lambda m: None)
        mock_run.assert_called_once()
        record = mock_run.call_args[0][1]
        self.assertEqual(record['attempted_sha'], SHA_A)
        self.assertEqual(record['attempt'], 2)


if __name__ == '__main__':
    unittest.main()
