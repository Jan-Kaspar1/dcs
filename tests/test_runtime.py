import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_pool.runtime import Runtime, atomic_json, is_alive, process_identity, runner


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.git('init', '-b', 'main')
        self.git('config', 'user.email', 'test@example.com')
        self.git('config', 'user.name', 'Test')
        (self.source / 'README').write_text('vision')
        self.git('add', 'README')
        self.git('commit', '-m', 'initial')
        self.runtime = Runtime(self.root / 'pool', self.root / 'state', str(self.source))

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return Runtime.run_git(self.source, *args)

    def test_dirty_clone_preserved_and_branch_isolated(self):
        clone = self.runtime.prepare_clone('worker-01', 'codex/issue-1-1')
        (clone / 'unfinished').write_text('preserve me')
        replacement = self.runtime.prepare_clone('worker-01', 'codex/issue-2-1')
        quarantines = list(self.runtime.pool_root.glob('worker-01-quarantine-*'))
        self.assertEqual(len(quarantines), 1)
        self.assertEqual((quarantines[0] / 'unfinished').read_text(), 'preserve me')
        self.assertFalse((replacement / 'unfinished').exists())
        result = self.runtime.inspect_result(replacement, 'codex/issue-2-1')
        self.assertTrue(result['clean'])
        self.assertFalse(result['changed'])
        self.assertEqual(self.git('branch', '--show-current'), 'main')

    def test_rejects_wrong_branch_and_path(self):
        clone = self.runtime.prepare_clone('worker-01', 'codex/issue-1-1')
        with self.assertRaises(ValueError):
            self.runtime.inspect_result(clone, 'other')
        with self.assertRaises(ValueError):
            self.runtime.prepare_clone('../escape')
        with self.assertRaises(ValueError):
            self.runtime.spawn('test', self.source, 'hello')

    def run_spec(self, command, timeout=2):
        spec = {'command': command, 'cwd': str(self.source), 'timeout': timeout,
                'log': str(self.root / 'log'), 'receipt': str(self.root / 'receipt.json'),
                'metadata': str(self.root / 'process.json')}
        path = self.root / 'spec.json'
        atomic_json(path, spec)
        runner(path)
        return json.loads(Path(spec['receipt']).read_text())

    def test_runner_completion_and_failure(self):
        result = self.run_spec([sys.executable, '-c', 'print("done")'])
        self.assertEqual(result['status'], 'completed')
        result = self.run_spec([sys.executable, '-c', 'raise SystemExit(7)'])
        self.assertEqual(result['exit_code'], 7)
        self.assertEqual(result['status'], 'failed')

    def test_runner_timeout(self):
        result = self.run_spec([sys.executable, '-c', 'import time; time.sleep(30)'], .1)
        self.assertEqual(result['status'], 'timeout')

    def test_process_identity_detects_reused_and_missing_pid(self):
        meta = {'pid': os.getpid(), 'identity': process_identity(os.getpid())}
        self.assertTrue(is_alive(meta))
        meta['identity'] += 'stale'
        self.assertFalse(is_alive(meta))
        self.assertIsNone(process_identity(999999999))

    def test_restart_reads_durable_completion(self):
        clone = self.runtime.prepare_clone('worker-01')
        self.runtime.devin = '/bin/true'
        metadata = self.runtime.spawn('worker-01', clone, 'test')
        restored = Runtime(self.runtime.pool_root, self.runtime.state_root, str(self.source))
        deadline = time.monotonic() + 5
        result = None
        while result is None and time.monotonic() < deadline:
            result = restored.poll(metadata)
            time.sleep(.05)
        self.assertEqual(result['status'], 'completed')
        self.runtime.poll(metadata)  # Reap child owned by original instance.
        spec = json.loads((Path(metadata['invocation']) / 'spec.json').read_text())
        self.assertIn('swe-2-high', spec['command'])
        self.assertIn('dangerous', spec['command'])
        self.assertNotIn('smart', spec['command'])

    def test_lost_process_is_not_success(self):
        result = self.runtime.poll({'pid': 999999999, 'identity': 'missing',
                                    'receipt': str(self.root / 'missing.json')})
        self.assertEqual(result['status'], 'lost')

    def test_recovery_excludes_known_records(self):
        invocation = self.runtime.state_root / 'worker-01-abc'
        invocation.mkdir()
        metadata = {'pid': os.getpid(), 'identity': process_identity(os.getpid()),
                    'invocation': str(invocation), 'cwd': str(self.source),
                    'receipt': str(invocation / 'receipt.json')}
        atomic_json(invocation / 'owner.json', metadata)
        self.assertEqual(self.runtime.recover(), [metadata])
        self.assertEqual(self.runtime.recover([str(invocation)]), [])

    def test_session_id_filters_directory_and_start_time(self):
        sessions = [{'id': 'older', 'working_directory': str(self.source), 'last_activity_at': 10},
                    {'id': 'latest', 'working_directory': str(self.source), 'last_activity_at': 20},
                    {'id': 'elsewhere', 'working_directory': '/elsewhere', 'last_activity_at': 30}]
        with patch('agent_pool.runtime.subprocess.run') as run:
            run.return_value.stdout = json.dumps(sessions)
            self.assertEqual(self.runtime.session_id(self.source, since=11), 'latest')
            self.assertIsNone(self.runtime.session_id(self.source, since=21))

    def test_spawn_refuses_live_checkout_owner(self):
        clone = self.runtime.prepare_clone('worker-01')
        invocation = self.runtime.state_root / 'old'
        invocation.mkdir()
        metadata = {'pid': os.getpid(), 'identity': process_identity(os.getpid()),
                    'invocation': str(invocation), 'cwd': str(clone), 'key': 'worker-01',
                    'receipt': str(invocation / 'receipt.json')}
        atomic_json(invocation / 'owner.json', metadata)
        with self.assertRaises(RuntimeError):
            self.runtime.spawn('worker-01', clone, 'do not run')

    def test_branch_reuse_does_not_replace_prior_commit(self):
        clone = self.runtime.prepare_clone('worker-01', 'codex/issue-1-1')
        with self.assertRaises(subprocess.CalledProcessError):
            self.runtime.prepare_clone('worker-01', 'codex/issue-1-1')
        self.assertEqual(self.runtime.run_git(clone, 'branch', '--show-current'), 'codex/issue-1-1')

    def test_restart_detects_running_runner_and_stop_receipt(self):
        spec = {'command': [sys.executable, '-c', 'import time; time.sleep(30)'],
                'cwd': str(self.source), 'timeout': 60,
                'log': str(self.root / 'log'), 'receipt': str(self.root / 'receipt.json'),
                'metadata': str(self.root / 'process.json')}
        path = self.root / 'spec.json'
        atomic_json(path, spec)
        from agent_pool import runtime
        process = subprocess.Popen([sys.executable, runtime.__file__, '--runner', str(path)],
                                   start_new_session=True)
        try:
            deadline = time.monotonic() + 5
            while not Path(spec['metadata']).exists() and time.monotonic() < deadline:
                time.sleep(.02)
            metadata = json.loads((self.root / 'owner.json').read_text())
            restored = Runtime(self.runtime.pool_root, self.runtime.state_root, str(self.source))
            self.assertIsNone(restored.poll(metadata))
            restored.terminate(metadata)
            process.wait(timeout=7)
            self.assertEqual(restored.poll(metadata)['status'], 'stopped')
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=7)


if __name__ == '__main__':
    unittest.main()
