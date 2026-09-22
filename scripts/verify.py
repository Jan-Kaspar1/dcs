#!/usr/bin/env python3
"""Repository checks; serialize heavy builds across local worker clones."""

import contextlib
import os
from pathlib import Path
import subprocess
import sys
import time

if os.name == "nt":
    import msvcrt
else:
    import fcntl


BUILD_SLOTS = 4


def _try_lock(handle):
    """Take the slot's exclusive lock without blocking; True when acquired."""
    if os.name == "nt":
        # msvcrt.locking locks nbytes from the current file position.
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _release(handle):
    """Drop the slot lock held by handle.

    POSIX keeps the lock until close on purpose — an interrupted parent
    can leave Cargo alive, and its inherited descriptor must retain the
    slot until that build exits — so there is no explicit unlock. Windows
    byte-range locks end with the locking process regardless, so unlock
    before close.
    """
    if os.name == "nt":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@contextlib.contextmanager
def build_slot(lock_dir=None):
    # Every clone uses the same per-user lock directory, outside the repository.
    lock_dir = Path(lock_dir) if lock_dir else Path.home() / ".local" / "state" / "dcs-agents" / "build-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    handles = [(lock_dir / f"slot-{index}.lock").open("a") for index in range(BUILD_SLOTS)]
    acquired = None
    started = time.monotonic()
    try:
        while acquired is None:
            for handle in handles:
                if _try_lock(handle):
                    acquired = handle
                    break
            if acquired is None:
                time.sleep(0.25)
        print(f"verify.py: {Path(acquired.name).stem} acquired after {time.monotonic() - started:.1f}s wait", flush=True)
        yield acquired
    finally:
        for handle in handles:
            if handle is acquired:
                _release(handle)
            handle.close()


def main():
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, CARGO_BUILD_JOBS="4", CARGO_TARGET_DIR=str(root / "target"))
    started = time.monotonic()
    subprocess.run(["cargo", "fmt", "--all", "--", "--check"], cwd=root, env=env, check=True)
    subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=root, env=env, check=True)
    with build_slot() as slot:
        held = time.monotonic()
        # pass_fds is POSIX-only; on Windows the byte-range lock ends with
        # this process whether or not the child would inherit the handle.
        inherit = {"pass_fds": (slot.fileno(),)} if os.name == "posix" else {}
        for args in (
            ["cargo", "clippy", "--workspace", "--all-targets", "--locked", "--", "-D", "warnings"],
            ["cargo", "test", "--workspace", "--locked"],
        ):
            subprocess.run(args, cwd=root, env=env, check=True, **inherit)
    print(f"verify.py: heavy builds held the slot {time.monotonic() - held:.1f}s; total {time.monotonic() - started:.1f}s", flush=True)


if __name__ == "__main__":
    main()
