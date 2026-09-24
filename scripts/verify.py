#!/usr/bin/env python3
"""Repository checks; serialize heavy builds across local worker clones."""

import argparse
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
PHASES = ("rust-format", "supervisor-tests", "rust-clippy", "rust-tests", "rust-proofs")
RUST_PHASES = frozenset(("rust-clippy", "rust-tests", "rust-proofs"))


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


def run_phase(name, args, root, env, **kwargs):
    """Run one required check and report its elapsed wall time."""
    started = time.monotonic()
    print(f"verify.py: start phase={name}", flush=True)
    status = "passed"
    try:
        subprocess.run(args, cwd=root, env=env, check=True, **kwargs)
    except BaseException:
        status = "failed"
        raise
    finally:
        elapsed = time.monotonic() - started
        print(f"verify.py: phase={name} status={status} elapsed_s={elapsed:.3f}", flush=True)
    return elapsed


def phase_command(name):
    """Return the command that implements a named required check."""
    commands = {
        "rust-format": ["cargo", "fmt", "--all", "--", "--check"],
        "supervisor-tests": [sys.executable, "scripts/run_tests.py", "--workers", "4"],
        "rust-clippy": ["cargo", "clippy", "--workspace", "--all-targets", "--locked", "--", "-D", "warnings"],
        "rust-tests": [sys.executable, "scripts/run_rust_tests.py", "--scope", "workspace"],
        "rust-proofs": [sys.executable, "scripts/run_rust_tests.py", "--scope", "proofs"],
    }
    return commands[name]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase",
        choices=PHASES,
        help="run one CI check phase; by default run every required phase",
    )
    options = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, CARGO_BUILD_JOBS="4", CARGO_TARGET_DIR=str(root / "target"))
    started = time.monotonic()
    if options.phase is None:
        for name in ("rust-format", "supervisor-tests"):
            run_phase(name, phase_command(name), root, env)
        with build_slot() as slot:
            held = time.monotonic()
            # pass_fds is POSIX-only; on Windows the byte-range lock ends with
            # this process whether or not the child would inherit the handle.
            inherit = {"pass_fds": (slot.fileno(),)} if os.name == "posix" else {}
            for name in ("rust-clippy", "rust-tests", "rust-proofs"):
                phase_env = env
                if name in ("rust-tests", "rust-proofs") and os.name == "posix":
                    phase_env = dict(env, DCS_BUILD_SLOT_FD=str(slot.fileno()))
                run_phase(name, phase_command(name), root, phase_env, **inherit)
        print(f"verify.py: cargo build slot held for {time.monotonic() - held:.1f}s", flush=True)
    else:
        name = options.phase
        if name in RUST_PHASES:
            with build_slot() as slot:
                held = time.monotonic()
                inherit = {"pass_fds": (slot.fileno(),)} if os.name == "posix" else {}
                phase_env = env
                if name in ("rust-tests", "rust-proofs") and os.name == "posix":
                    phase_env = dict(env, DCS_BUILD_SLOT_FD=str(slot.fileno()))
                run_phase(name, phase_command(name), root, phase_env, **inherit)
            print(f"verify.py: cargo build slot held for {time.monotonic() - held:.1f}s", flush=True)
        else:
            run_phase(name, phase_command(name), root, env)

    print(f"verify.py: total elapsed_s={time.monotonic() - started:.3f}", flush=True)


if __name__ == "__main__":
    main()
