#!/usr/bin/env python3
"""Run workspace Rust tests, split by gate scope.

The "workspace" scope is the fast required-PR leg: the whole workspace
suite minus the nested clean-target proofs. The "proofs" scope is the
release-assembly legs, each a targeted rerun of one skipped proof. The
default "all" scope runs both — the local full gate.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
from pathlib import Path
import subprocess
import sys
import threading
import time


# Keep each expensive proof's exclusion and targeted rerun in one source.
NESTED_PROOFS = (
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
    (
        "reference-template",
        "reference_plant",
        "the_template_passes_its_own_clean_ci_outside_the_workspace",
    ),
    (
        "reference-upgrade",
        "reference_plant",
        "the_upgrade_stage_proves_the_repin_and_the_named_crossings",
    ),
)

OUTPUT_LOCK = threading.Lock()


def command_plan(scope="all"):
    """Return the commands one gate scope runs, keyed by task label."""
    plan = {}
    if scope in ("all", "workspace"):
        workspace = ["cargo", "test", "--workspace", "--locked", "--"]
        for _, _, test_name in NESTED_PROOFS:
            workspace.extend(("--skip", test_name))
        plan["workspace"] = workspace
    if scope in ("all", "proofs"):
        for label, target, test_name in NESTED_PROOFS:
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
    if label in {proof[0] for proof in NESTED_PROOFS}:
        # Scratch consumer and reference builds run concurrently; one build job
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scope",
        choices=("all", "workspace", "proofs"),
        default="all",
        help="which gate scope to run; default runs every leg",
    )
    options = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[1]
    plan = command_plan(options.scope)
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
