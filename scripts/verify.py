#!/usr/bin/env python3
"""Repository checks; serialize heavy builds across local worker clones."""

import contextlib
import fcntl
import os
from pathlib import Path
import subprocess
import time


BUILD_SLOTS = 4


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
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = handle
                    break
                except BlockingIOError:
                    continue
            if acquired is None:
                time.sleep(0.25)
        print(f"verify.py: {Path(acquired.name).stem} acquired after {time.monotonic() - started:.1f}s wait", flush=True)
        yield acquired
    finally:
        # Close instead of LOCK_UN: an interrupted parent can leave Cargo alive;
        # its inherited descriptor must retain the slot until that build exits.
        for handle in handles:
            handle.close()


def main():
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, CARGO_BUILD_JOBS="4", CARGO_TARGET_DIR=str(root / "target"))
    started = time.monotonic()
    subprocess.run(["cargo", "fmt", "--all", "--", "--check"], cwd=root, env=env, check=True)
    subprocess.run(["python3", "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=root, env=env, check=True)
    with build_slot() as slot:
        held = time.monotonic()
        for args in (
            ["cargo", "clippy", "--workspace", "--all-targets", "--locked", "--", "-D", "warnings"],
            ["cargo", "test", "--workspace", "--locked"],
        ):
            subprocess.run(args, cwd=root, env=env, check=True, pass_fds=(slot.fileno(),))
    print(f"verify.py: heavy builds held the slot {time.monotonic() - held:.1f}s; total {time.monotonic() - started:.1f}s", flush=True)


if __name__ == "__main__":
    main()
