"""scripts/verify.py build-slot gate: the lock primitive must acquire,
exclude, and release a slot on the running platform (fcntl on POSIX,
msvcrt byte-range locking on Windows)."""
import os
import tempfile
import unittest
from pathlib import Path

from scripts import verify


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
                with patch('os.name', 'nt'), \
                        patch.dict(sys.modules, {'msvcrt': fake}):
                    spec.loader.exec_module(mod)
                    self.assertTrue(mod._try_lock(held))
                    self.assertFalse(mod._try_lock(rival))
                    mod._release(held)
                    self.assertTrue(mod._try_lock(reacquired))
            finally:
                held.close()
                rival.close()
                reacquired.close()


if __name__ == '__main__':
    unittest.main()
