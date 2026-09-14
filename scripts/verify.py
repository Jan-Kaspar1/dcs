#!/usr/bin/env python3
"""Repository checks; serialize heavy builds across local worker clones."""

import contextlib
import fcntl
import os
from pathlib import Path
import subprocess
import time


@contextlib.contextmanager
def build_slot():
    # Every clone uses the same per-user lock directory, outside the repository.
    lock_dir = Path.home() / ".local" / "state" / "dcs-agents" / "build-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    handles = [(lock_dir / f"slot-{index}.lock").open("a") for index in range(2)]
    acquired = None
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
        yield acquired
    finally:
        # Close instead of LOCK_UN: an interrupted parent can leave Cargo alive;
        # its inherited descriptor must retain the slot until that build exits.
        for handle in handles:
            handle.close()


def main():
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, CARGO_BUILD_JOBS="4", CARGO_TARGET_DIR=str(root / "target"))
    subprocess.run(["cargo", "fmt", "--all", "--", "--check"], cwd=root, env=env, check=True)
    subprocess.run(["python3", "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=root, env=env, check=True)
    with build_slot() as slot:
        for args in (
            ["cargo", "clippy", "--workspace", "--all-targets", "--locked", "--", "-D", "warnings"],
            ["cargo", "test", "--workspace", "--locked"],
        ):
            subprocess.run(args, cwd=root, env=env, check=True, pass_fds=(slot.fileno(),))


if __name__ == "__main__":
    main()
