"""scripts/verify.py gates: phase timing and build-slot behavior."""
import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import verify


class PhaseTimingTests(unittest.TestCase):
    def test_run_phase_logs_elapsed_time_and_preserves_command(self):
        output = io.StringIO()
        root = Path(".")
        env = {"CARGO_BUILD_JOBS": "4"}
        command = ["cargo", "test", "--workspace", "--locked"]
        with (
            patch.object(verify.time, "monotonic", side_effect=(3.0, 4.25)),
            patch.object(verify.subprocess, "run") as run,
            contextlib.redirect_stdout(output),
        ):
            elapsed = verify.run_phase("rust-tests", command, root, env)

        self.assertEqual(elapsed, 1.25)
        run.assert_called_once_with(command, cwd=root, env=env, check=True)
        self.assertIn("phase=rust-tests status=passed elapsed_s=1.250", output.getvalue())

    def test_run_phase_logs_failure_and_propagates_it(self):
        output = io.StringIO()
        error = subprocess.CalledProcessError(1, ["cargo", "clippy"])
        with (
            patch.object(verify.time, "monotonic", side_effect=(5.0, 6.5)),
            patch.object(verify.subprocess, "run", side_effect=error),
            contextlib.redirect_stdout(output),
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                verify.run_phase("rust-clippy", ["cargo", "clippy"], Path("."), {})

        self.assertIn("phase=rust-clippy status=failed elapsed_s=1.500", output.getvalue())

    def test_ci_phase_commands_match_required_checks(self):
        self.assertEqual(verify.phase_command("rust-format"), [
            "cargo", "fmt", "--all", "--", "--check",
        ])
        self.assertEqual(verify.phase_command("rust-clippy"), [
            "cargo", "clippy", "--workspace", "--all-targets", "--locked",
            "--", "-D", "warnings",
        ])
        self.assertEqual(verify.phase_command("rust-tests"), [
            "cargo", "test", "--workspace", "--locked",
        ])
        self.assertEqual(verify.phase_command("supervisor-tests"), [
            verify.sys.executable, "scripts/run_tests.py", "--workers", "4",
        ])


class BuildSlotTests(unittest.TestCase):
    def test_lock_acquires_excludes_and_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            slot = Path(tmp) / 'slot-0.lock'
            with slot.open('a') as held:
                self.assertTrue(verify._try_lock(held))
                with slot.open('a') as rival:
                    # A second open of the same file must not take the lock.
                    self.assertFalse(verify._try_lock(rival))
                verify._release(held)
            with slot.open('a') as reacquired:
                self.assertTrue(verify._try_lock(reacquired))
                verify._release(reacquired)

    def test_build_slot_context_acquires_and_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            with verify.build_slot(lock_dir=tmp) as held:
                self.assertTrue(Path(held.name).name.startswith('slot-'))
                self.assertEqual(
                    len(list(Path(tmp).glob('slot-*.lock'))),
                    verify.BUILD_SLOTS)
                with Path(held.name).open('a') as rival:
                    self.assertFalse(verify._try_lock(rival))
            with Path(held.name).open('a') as reacquired:
                self.assertTrue(verify._try_lock(reacquired))
                verify._release(reacquired)

    @unittest.skipUnless(os.name == 'posix',
                         'the fake msvcrt is backed by flock')
    def test_windows_lock_branch(self):
        """Exercise the nt code path on a POSIX host: reload verify.py with
        a fake msvcrt (implemented over flock) so CI covers the branch real
        Windows takes."""
        import fcntl
        import importlib.util
        import sys
        import types
        from unittest.mock import patch

        fake = types.SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2)

        def locking(fd, mode, nbytes):
            if mode == fake.LK_NBLCK:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif mode == fake.LK_UNLCK:
                fcntl.flock(fd, fcntl.LOCK_UN)

        fake.locking = locking
        spec = importlib.util.spec_from_file_location(
            'verify_nt', verify.__file__)
        mod = importlib.util.module_from_spec(spec)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'slot-0.lock'
            held, rival, reacquired = (path.open('a') for _ in range(3))
            try:
                with (
                    patch('os.name', 'nt'),
                    patch.dict(sys.modules, {'msvcrt': fake}),
                ):
                    spec.loader.exec_module(mod)
                    self.assertTrue(mod._try_lock(held))
                    self.assertFalse(mod._try_lock(rival))
                    mod._release(held)
                    self.assertTrue(mod._try_lock(reacquired))
            finally:
                held.close()
                rival.close()
                reacquired.close()


if __name__ == "__main__":
    unittest.main()
