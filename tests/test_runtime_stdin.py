"""OpenCode prompt transport over stdin (issue #814).

Regression: Runtime.spawn appended the full prompt to argv, so a large
backlog context exceeded Linux ARG_MAX and the launch failed with E2BIG.
OpenCode natively consumes piped stdin as the prompt, so the runner now
opens the durable prompt.txt as the child stdin and argv carries only
fixed flags. Devin keeps its --prompt-file transport.
"""
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest

from agent_pool.runtime import Runtime, atomic_json, runner

posix_only = unittest.skipUnless(os.name == 'posix', 'agent_pool runtime is Linux/WSL only')

# Multi-megabyte prompt: exceeds per-string MAX_ARG_STRLEN (128 KiB) and the
# platform's ARG_MAX budget — sized dynamically because the limit varies by
# host (2 MiB here, 4 MiB on the CI runner) — so placing it in argv would
# raise E2BIG.
LARGE_PREFIX = 'stdin-regression-marker-814-'


def arg_max():
    try:
        return os.sysconf('SC_ARG_MAX')
    except (AttributeError, ValueError, OSError):
        return 2 * 1024 * 1024


def large_prompt():
    size = arg_max() + 1024 * 1024
    padding = size - len(LARGE_PREFIX) - len('-tail')
    return LARGE_PREFIX + 'X' * padding + '-tail'


class OpenCodeStdinTests(unittest.TestCase):
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

    def wait_completed(self, metadata, timeout=15):
        deadline = time.monotonic() + timeout
        result = None
        while result is None and time.monotonic() < deadline:
            result = self.runtime.poll(metadata)
            if result is None:
                time.sleep(.05)
        return result

    def make_stdin_double(self, argv_dump, stdin_capture):
        """Test double mimicking `opencode run` stdin consumption."""
        script = self.root / ('fake-opencode-' + argv_dump.stem)
        script.write_text(
            '#!/usr/bin/env python3\n'
            'import sys\n'
            'from pathlib import Path\n'
            f'Path({str(argv_dump)!r}).write_text("\\x00".join(sys.argv[1:]))\n'
            f'Path({str(stdin_capture)!r}).write_bytes(sys.stdin.buffer.read())\n'
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return str(script)

    @posix_only
    def test_large_prompt_exceeds_arg_limit_but_spawns_without_argv_prompt(self):
        prompt = large_prompt()
        self.assertGreater(len(prompt.encode()), arg_max(),
                           'fixture must exceed the platform argument limit')
        clone = self.runtime.prepare_clone('worker-01')
        self.runtime.opencode = '/bin/true'
        metadata = self.runtime.spawn('worker-01', clone, prompt,
                                      model='opencode/muse-spark-1.3-contributor-free')
        try:
            result = self.wait_completed(metadata)
            self.assertEqual(result['status'], 'completed')
            invocation = Path(metadata['invocation'])
            spec = json.loads((invocation / 'spec.json').read_text())
            # Fixed flags only; no prompt text in argv.
            self.assertNotIn(prompt, ' '.join(spec['command']))
            for part in spec['command']:
                self.assertNotIn(LARGE_PREFIX, part)
            self.assertIn('--print-logs', spec['command'])
            self.assertIn('--title', spec['command'])
            # Prompt travels as a file path, never as file contents.
            self.assertEqual(spec.get('stdin'), str(invocation / 'prompt.txt'))
            spec_text = (invocation / 'spec.json').read_text()
            self.assertNotIn(LARGE_PREFIX, spec_text)
            owner_text = (invocation / 'owner.json').read_text()
            self.assertNotIn(LARGE_PREFIX, owner_text)
            self.assertEqual((invocation / 'prompt.txt').read_text(), prompt)
        finally:
            self.runtime.poll(metadata)

    @posix_only
    def test_large_prompt_received_byte_for_byte_on_stdin(self):
        prompt = large_prompt()
        clone = self.runtime.prepare_clone('worker-01')
        argv_dump = self.root / 'argv.txt'
        stdin_capture = self.root / 'stdin.bin'
        self.runtime.opencode = self.make_stdin_double(argv_dump, stdin_capture)
        metadata = self.runtime.spawn('worker-01', clone, prompt,
                                      model='opencode/muse-spark-1.3-contributor-free')
        try:
            result = self.wait_completed(metadata)
            self.assertIsNotNone(result)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(stdin_capture.read_bytes(), prompt.encode())
            self.assertNotIn(LARGE_PREFIX, argv_dump.read_text())
        finally:
            self.runtime.poll(metadata)

    @posix_only
    def test_opencode_resume_keeps_session_and_stdin(self):
        clone = self.runtime.prepare_clone('worker-01')
        self.runtime.opencode = '/bin/true'
        metadata = self.runtime.spawn('worker-01', clone, 'resume me',
                                      model='opencode/muse-spark-1.3-contributor-free',
                                      resume_session='ses_abc123')
        try:
            spec = json.loads((Path(metadata['invocation']) / 'spec.json').read_text())
            self.assertIn('--session', spec['command'])
            self.assertIn('ses_abc123', spec['command'])
            self.assertNotIn('resume me', ' '.join(spec['command']))
            self.assertTrue(spec.get('stdin', '').endswith('prompt.txt'))
            self.assertEqual(Path(spec['stdin']).read_text(), 'resume me')
            result = self.wait_completed(metadata)
            self.assertEqual(result['status'], 'completed')
        finally:
            self.runtime.poll(metadata)

    def test_devin_keeps_prompt_file_transport(self):
        clone = self.runtime.prepare_clone('worker-01')
        self.runtime.devin = '/bin/true'
        metadata = self.runtime.spawn('worker-01', clone, 'devin prompt', model='swe-2-medium')
        try:
            spec = json.loads((Path(metadata['invocation']) / 'spec.json').read_text())
            self.assertIn('--prompt-file', spec['command'])
            prompt_file = spec['command'][spec['command'].index('--prompt-file') + 1]
            self.assertEqual(Path(prompt_file).read_text(), 'devin prompt')
            self.assertIsNone(spec.get('stdin'))
            result = self.wait_completed(metadata)
            self.assertEqual(result['status'], 'completed')
        finally:
            self.runtime.poll(metadata)

    @posix_only
    def test_runner_stdin_completion_receipt_and_logging(self):
        prompt_path = self.root / 'prompt.txt'
        prompt_path.write_text('hello stdin')
        log = self.root / 'log'
        receipt = self.root / 'receipt.json'
        process = self.root / 'process.json'
        spec = {'command': [sys.executable, '-c',
                            'import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())'],
                'cwd': str(self.source), 'timeout': 10, 'stdin': str(prompt_path),
                'log': str(log), 'receipt': str(receipt), 'metadata': str(process)}
        spec_path = self.root / 'spec.json'
        atomic_json(spec_path, spec)
        runner(spec_path)
        result = json.loads(receipt.read_text())
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(log.read_bytes(), b'hello stdin')

    @posix_only
    def test_runner_stdin_preserves_stall_watchdog(self):
        prompt_path = self.root / 'prompt2.txt'
        prompt_path.write_text('watchdog prompt')
        log = self.root / 'log2'
        receipt = self.root / 'receipt2.json'
        process = self.root / 'process2.json'
        spec = {'command': [sys.executable, '-c', 'import time; time.sleep(30)'],
                'cwd': str(self.source), 'timeout': 30, 'stall_seconds': .5,
                'error_stall_seconds': .3, 'stdin': str(prompt_path),
                'log': str(log), 'receipt': str(receipt), 'metadata': str(process)}
        spec_path = self.root / 'spec2.json'
        atomic_json(spec_path, spec)
        started = time.monotonic()
        runner(spec_path)
        result = json.loads(receipt.read_text())
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('hang', result.get('error', ''))

    @posix_only
    def test_runner_without_stdin_stays_devnull(self):
        log = self.root / 'log3'
        receipt = self.root / 'receipt3.json'
        process = self.root / 'process3.json'
        spec = {'command': [sys.executable, '-c',
                            'import sys; data=sys.stdin.buffer.read(); '
                            'print(len(data))'],
                'cwd': str(self.source), 'timeout': 10,
                'log': str(log), 'receipt': str(receipt), 'metadata': str(process)}
        spec_path = self.root / 'spec3.json'
        atomic_json(spec_path, spec)
        runner(spec_path)
        result = json.loads(receipt.read_text())
        self.assertEqual(result['status'], 'completed')
        self.assertIn(b'0', log.read_bytes())


if __name__ == '__main__':
    unittest.main()
