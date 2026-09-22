"""Isolated checkouts and restart-safe local process execution (Linux/WSL)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import time
import uuid


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix('.tmp-' + uuid.uuid4().hex)
    with temporary.open('w') as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def process_identity(pid):
    try:
        stat = Path(f'/proc/{pid}/stat').read_text()
        fields = stat[stat.rfind(')') + 2:].split()
        if fields[0] == 'Z':
            return None
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip() + ':' + fields[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


def is_alive(metadata):
    return bool(metadata.get('identity')) and process_identity(metadata['pid']) == metadata['identity']


class Runtime:
    def __init__(self, pool_root, state_root, repository, devin='devin', opencode='opencode', timeout_seconds=7200):
        self.pool_root = Path(pool_root).resolve()
        self.state_root = Path(state_root).resolve()
        self.repository = repository
        self.devin = devin
        self.opencode = opencode
        self.opencode_db = Path.home() / '.local/share/opencode/opencode.db'
        self.stall_seconds = 1200
        self.error_stall_seconds = 180
        self.timeout_seconds = timeout_seconds
        self.pool_root.mkdir(parents=True, exist_ok=True)
        self.state_root.mkdir(parents=True, exist_ok=True)
        self._children = {}

    @staticmethod
    def run_git(cwd, *args):
        return subprocess.run(['git', '-C', str(cwd), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def prepare_clone(self, name, branch=None):
        if not re.fullmatch(r'[A-Za-z0-9_-]+', name):
            raise ValueError('Invalid managed clone name')
        clone = self.pool_root / name
        if self.clone_owned(clone):
            raise RuntimeError('Cannot prepare a checkout owned by a live invocation')
        if clone.exists():
            dirty = self.run_git(clone, 'status', '--porcelain')
            if dirty:
                clone.rename(self.pool_root / (name + '-quarantine-' + uuid.uuid4().hex))
        if not clone.exists():
            source = self.repository
            if not source.startswith(('/', 'https://', 'git@', 'file://')):
                source = 'https://github.com/' + source + '.git'
            subprocess.run(['git', 'clone', source, str(clone)], check=True,
                           capture_output=True, text=True)
        self.run_git(clone, 'fetch', 'origin', 'main')
        if branch:
            self.run_git(clone, 'check-ref-format', '--branch', branch)
            self.run_git(clone, 'switch', '-c', branch, 'origin/main')
        else:
            self.run_git(clone, 'switch', '--detach', 'origin/main')
        return clone

    def spawn(self, key, cwd, prompt, resume_session=None, timeout=None, model='swe-2-high'):
        if not re.fullmatch(r'[A-Za-z0-9_-]+', key):
            raise ValueError('Invalid invocation key')
        cwd = Path(cwd).resolve()
        if not cwd.is_relative_to(self.pool_root):
            raise ValueError('Agent directory must be a managed checkout')
        for previous in self.recover():
            if previous.get('key') == key or previous.get('cwd') == str(cwd):
                if self.poll(previous) is None or self.child_alive(previous):
                    raise RuntimeError('An invocation already owns this worker or checkout')
        invocation = self.state_root / (key + '-' + uuid.uuid4().hex)
        invocation.mkdir()
        prompt_path = invocation / 'prompt.txt'
        prompt_path.write_text(prompt)
        if model.startswith('opencode/'):
            # --print-logs mirrors provider stream errors into the invocation
            # log; without it opencode freezes silently after a rate-limited
            # stream and only the hard timeout notices. The prompt travels
            # via stdin (the durable prompt.txt opened by the runner), never
            # argv: large backlog contexts exceed Linux ARG_MAX and the
            # launch fails with E2BIG before opencode starts.
            command = [self.opencode, 'run', '--model', model, '--auto',
                       '--print-logs', '--title', key]
            if resume_session:
                command.extend(['--session', resume_session])
            stdin_path = str(prompt_path)
            stall = self.stall_seconds
            error_stall = self.error_stall_seconds
        else:
            stall = error_stall = None
            command = [self.devin, '-p', '--model', model, '--permission-mode',
                       'dangerous', '--respect-workspace-trust', 'false', '--prompt-file',
                       str(prompt_path), '--export', str(invocation / 'conversation.json')]
            if resume_session:
                command.extend(['--resume', resume_session])
            stdin_path = None
        spec = {'key': key, 'command': command, 'cwd': str(cwd), 'timeout': timeout or self.timeout_seconds,
                'stall_seconds': stall, 'error_stall_seconds': error_stall,
                'stdin': stdin_path,
                'receipt': str(invocation / 'receipt.json'), 'log': str(invocation / 'output.log'),
                'metadata': str(invocation / 'process.json')}
        atomic_json(invocation / 'spec.json', spec)
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                    '--runner', str(invocation / 'spec.json')],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        self._children[process.pid] = process
        metadata = {**spec, 'pid': process.pid, 'identity': process_identity(process.pid),
                    'started_at': time.time(), 'invocation': str(invocation)}
        atomic_json(invocation / 'owner.json', metadata)
        return metadata

    def clone_owned(self, path):
        """Whether a live managed invocation owns this checkout directory."""
        target = str(Path(path).resolve())
        for record in self.recover():
            if record.get('cwd') == target:
                if self.poll(record) is None or self.child_alive(record):
                    return True
        return False

    def recover(self, known_invocations=()):
        """Return durable invocation owners not accounted for by supervisor state.

        Both completed and active records are returned: the supervisor decides
        whether to adopt or block them. Malformed records fail closed.
        """
        known = {str(Path(path).resolve()) for path in known_invocations}
        records = []
        for path in sorted(self.state_root.glob('*/owner.json')):
            if str(path.parent.resolve()) in known:
                continue
            record = json.loads(path.read_text())
            if not {'pid', 'identity', 'receipt', 'invocation', 'cwd'} <= record.keys():
                raise RuntimeError(f'Incomplete process owner record: {path}')
            if Path(record['invocation']).resolve() != path.parent.resolve():
                raise RuntimeError(f'Inconsistent process owner record: {path}')
            records.append(record)
        return records

    @staticmethod
    def child_alive(metadata):
        path = Path(metadata.get('metadata', '/nonexistent'))
        return path.is_file() and is_alive(json.loads(path.read_text()))

    def session_id(self, cwd, since=None, model='swe-2-high', key=None):
        """Find the latest session for this invocation, optionally since launch."""
        if model.startswith('opencode/'):
            return self._opencode_session_id(key, cwd)
        result = subprocess.run([self.devin, 'list', '--format', 'json'], cwd=str(cwd),
                                check=True, capture_output=True, text=True, timeout=30)
        sessions = json.loads(result.stdout)
        if not isinstance(sessions, list):
            raise ValueError('Unexpected Devin session list')
        directory = Path(cwd).resolve()
        matching = [session for session in sessions
                    if isinstance(session, dict) and isinstance(session.get('id'), str)
                    and Path(session.get('working_directory', '')).resolve() == directory
                    and isinstance(session.get('last_activity_at'), (int, float))
                    and (since is None or session['last_activity_at'] >= int(since))]
        return max(matching, key=lambda item: item['last_activity_at'])['id'] if matching else None

    def _opencode_session_id(self, key, cwd=None):
        """Locate the opencode session spawned for an invocation key.

        Spawns pass --title <key>, and opencode registers pool runs under the
        global project with the home directory — so the invocation key, not
        the checkout path, is the reliable identity. Resume is best-effort:
        any lookup failure just means the next launch starts a new session.
        """
        if not key:
            return None
        try:
            db = sqlite3.connect('file:' + str(self.opencode_db) + '?mode=ro',
                                 uri=True, timeout=5)
            try:
                row = db.execute('SELECT id FROM session WHERE title=? '
                                 'ORDER BY time_created DESC LIMIT 1', (key,)).fetchone()
            finally:
                db.close()
        except sqlite3.Error:
            return None
        return row[0] if row else None

    def poll(self, metadata):
        child = self._children.get(metadata['pid'])
        if child is not None and child.poll() is not None:
            self._children.pop(metadata['pid'], None)
        receipt = Path(metadata['receipt'])
        if receipt.exists():
            return json.loads(receipt.read_text())
        if is_alive(metadata):
            return None
        return {'status': 'lost', 'exit_code': None,
                'error': 'Runner disappeared without a completion receipt; inspect preserved work'}

    def terminate(self, metadata):
        if is_alive(metadata):
            os.kill(metadata['pid'], signal.SIGTERM)
        else:
            child_record = Path(metadata.get('metadata', '/nonexistent'))
            if child_record.is_file():
                child = json.loads(child_record.read_text())
                if is_alive(child):
                    os.killpg(child['pid'], signal.SIGTERM)

    def inspect_result(self, cwd, expected_branch):
        branch = self.run_git(cwd, 'branch', '--show-current')
        if branch != expected_branch:
            raise ValueError('Worker changed the assigned branch')
        head = self.run_git(cwd, 'rev-parse', 'HEAD')
        base = self.run_git(cwd, 'rev-parse', 'origin/main')
        return {'head': head, 'base': base,
                'changed': bool(self.run_git(cwd, 'diff', '--name-only', 'origin/main...HEAD')),
                'clean': not bool(self.run_git(cwd, 'status', '--porcelain'))}


def _tail_has(path, needle, size=4096):
    try:
        with open(path, 'rb') as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - size))
            return needle in stream.read().decode(errors='replace').lower()
    except OSError:
        return False


def runner(spec_path):
    spec = json.loads(Path(spec_path).read_text())
    # The runner writes its own owner record as well, closing the window in
    # which the supervisor dies between Popen and saving the returned metadata.
    invocation = Path(spec_path).parent
    atomic_json(invocation / 'owner.json', {
        **spec, 'pid': os.getpid(), 'identity': process_identity(os.getpid()),
        'started_at': time.time(), 'invocation': str(invocation)})
    interrupted = False
    child = None

    def stop(signum, frame):
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    status, code = 'failed', None
    started = time.monotonic()
    try:
        with open(spec['log'], 'ab', buffering=0) as log:
            environment = os.environ.copy()
            environment['CARGO_BUILD_JOBS'] = '4'
            environment['CARGO_TARGET_DIR'] = str(Path(spec['cwd']) / 'target')
            stdin_path = spec.get('stdin')
            stdin_handle = None
            try:
                if stdin_path:
                    stdin_handle = open(stdin_path, 'rb')
                    stdin_arg = stdin_handle
                else:
                    stdin_arg = subprocess.DEVNULL
                child = subprocess.Popen(spec['command'], cwd=spec['cwd'], env=environment,
                                         stdin=stdin_arg, stdout=log, stderr=log,
                                         start_new_session=True)
            finally:
                if stdin_handle is not None:
                    stdin_handle.close()
            atomic_json(spec['metadata'], {'pid': child.pid, 'identity': process_identity(child.pid)})
            stall_seconds = spec.get('stall_seconds')
            error_stall = spec.get('error_stall_seconds') or stall_seconds
            last_mtime, last_activity, stalled = None, time.monotonic(), None
            while child.poll() is None:
                reason = None
                if interrupted or time.monotonic() - started >= spec['timeout']:
                    reason = 'stopped' if interrupted else 'timeout'
                elif stall_seconds:
                    try:
                        mtime = os.path.getmtime(spec['log'])
                    except OSError:
                        mtime = last_mtime
                    if mtime != last_mtime:
                        last_mtime, last_activity = mtime, time.monotonic()
                    idle = time.monotonic() - last_activity
                    # A provider 'stream error' as the most recent log event means
                    # the agent froze on a dead stream; fail it fast instead of
                    # waiting out the generic stall or hard timeout windows.
                    if idle >= stall_seconds or (idle >= error_stall and
                                                 _tail_has(spec['log'], 'stream error')):
                        reason = 'failed'
                        stalled = 'No agent output for %ds; treating as hang' % int(idle)
                if reason is None:
                    time.sleep(0.1)
                    continue
                status = reason
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                # Kill any descendants even if the group leader exited after TERM.
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                break
            code = child.wait()
            if status not in ('stopped', 'timeout'):
                status = 'completed' if code == 0 else 'failed'
            if stalled:
                status = 'failed'
    except Exception as error:
        atomic_json(spec['receipt'], {'status': 'failed', 'exit_code': code,
                                     'error': str(error), 'finished_at': time.time()})
        return
    receipt = {'status': status, 'exit_code': code, 'finished_at': time.time()}
    if stalled:
        receipt['error'] = stalled
    atomic_json(spec['receipt'], receipt)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--runner':
        runner(sys.argv[2])
    else:
        raise SystemExit('Internal process runner; use dcs-agents')
