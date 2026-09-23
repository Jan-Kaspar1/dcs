#!/usr/bin/env python3
"""Run all workspace Rust tests while splitting out the two nested-Cargo proofs."""

from concurrent.futures import ThreadPoolExecutor, as_completed
import os
from pathlib import Path
import subprocess
import sys
import threading
import time


# Keep each expensive proof's exclusion and targeted rerun in one source.
CONSUMER_PROOFS = (
    (
        "consumer-release",
        "consumer_release",
        "consumer_resolves_composes_emits_and_passes_released_tooling",
    ),
    (
        "consumer-upgrade",
        "consumer_upgrade",
        "a_repin_within_the_minor_series_is_a_drop_in_upgrade",
    ),
)

OUTPUT_LOCK = threading.Lock()


def command_plan():
    workspace = ["cargo", "test", "--workspace", "--locked", "--"]
    for _, _, test_name in CONSUMER_PROOFS:
        workspace.extend(("--skip", test_name))

    plan = {"workspace": workspace}
    for label, target, test_name in CONSUMER_PROOFS:
        plan[label] = [
            "cargo",
            "test",
            "-p",
            "dcs-build",
            "--test",
            target,
            "--locked",
            test_name,
        ]
    return plan


def command_environment(label, root):
    env = dict(os.environ, CARGO_TARGET_DIR=str(root / "target"))
    if label in {proof[0] for proof in CONSUMER_PROOFS}:
        # The two scratch consumer builds run concurrently; one build job
        # each avoids oversubscribing the CI runner.
        env["CARGO_BUILD_JOBS"] = "1"
    return env


def inherited_lock_fds():
    if os.name != "posix":
        return ()
    value = os.environ.get("DCS_BUILD_SLOT_FD")
    return (int(value),) if value is not None else ()


def run_command(label, command, root, env, pass_fds):
    started = time.monotonic()
    with OUTPUT_LOCK:
        print(f"run_rust_tests.py: start task={label}", flush=True)

    options = {}
    if os.name == "posix":
        options["pass_fds"] = pass_fds
    process = subprocess.Popen(
        command,
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        **options,
    )
    try:
        assert process.stdout is not None
        for line in process.stdout:
            with OUTPUT_LOCK:
                print(f"[{label}] {line}", end="", flush=True)
        returncode = process.wait()
    except BaseException:
        process.terminate()
        process.wait()
        raise

    elapsed = time.monotonic() - started
    status = "passed" if returncode == 0 else "failed"
    with OUTPUT_LOCK:
        print(
            f"run_rust_tests.py: task={label} status={status} "
            f"elapsed_s={elapsed:.3f}",
            flush=True,
        )
    return returncode


def main():
    root = Path(__file__).resolve().parents[1]
    plan = command_plan()
    pass_fds = inherited_lock_fds()
    started = time.monotonic()
    failed = False

    with ThreadPoolExecutor(max_workers=len(plan)) as pool:
        futures = {
            pool.submit(
                run_command,
                label,
                command,
                root,
                command_environment(label, root),
                pass_fds,
            ): label
            for label, command in plan.items()
        }
        for future in as_completed(futures):
            label = futures[future]
            try:
                failed = future.result() != 0 or failed
            except Exception as error:
                with OUTPUT_LOCK:
                    print(
                        f"run_rust_tests.py: task={label} "
                        f"status=failed error={error}",
                        flush=True,
                    )
                failed = True

    elapsed = time.monotonic() - started
    status = "failed" if failed else "passed"
    print(
        f"run_rust_tests.py: status={status} elapsed_s={elapsed:.3f}",
        flush=True,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
