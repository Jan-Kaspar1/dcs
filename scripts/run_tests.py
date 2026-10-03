#!/usr/bin/env python3
"""Run the discovered supervisor tests in deterministic, class-preserving shards."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import subprocess
import sys
import time
import unittest


def iter_test_cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from iter_test_cases(item)
        else:
            yield item


def shard_test_cases(test_cases, worker_count):
    """Balance test classes across workers without splitting class fixtures."""
    if worker_count < 1:
        raise ValueError("worker_count must be positive")

    groups = {}
    for position, test in enumerate(test_cases):
        test_type = type(test)
        key = f"{test_type.__module__}.{test_type.__qualname__}"
        groups.setdefault(key, []).append((position, test))

    shards = [[] for _ in range(worker_count)]
    loads = [0] * worker_count
    for key, group in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0])):
        worker = min(range(worker_count), key=lambda index: (loads[index], index))
        shards[worker].extend(group)
        loads[worker] += len(group)

    result = []
    for shard in shards:
        shard.sort(key=lambda pair: pair[0])
        result.append([test for _, test in shard])
    return result


def discover_test_cases(root, pattern):
    root = Path(root).resolve()
    tests_dir = root / "tests"
    for path in (root, tests_dir):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    suite = unittest.defaultTestLoader.discover(
        start_dir=str(tests_dir),
        pattern=pattern,
        top_level_dir=str(tests_dir),
    )
    return list(iter_test_cases(suite))


def run_worker(root, worker, worker_count, pattern):
    started = time.monotonic()
    shards = shard_test_cases(discover_test_cases(root, pattern), worker_count)
    suite = unittest.TestSuite(shards[worker])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    elapsed = time.monotonic() - started
    status = "passed" if result.wasSuccessful() else "failed"
    print(
        f"run_tests.py: shard={worker + 1}/{worker_count} "
        f"tests={result.testsRun} status={status} elapsed_s={elapsed:.3f}",
        flush=True,
    )
    return 0 if result.wasSuccessful() else 1


def run_shard_process(root, worker, worker_count, pattern):
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        str(worker),
        "--workers",
        str(worker_count),
        "--pattern",
        pattern,
    ]
    started = time.monotonic()
    result = subprocess.run(
        command,
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    return worker, result, time.monotonic() - started


def run_coordinator(root, worker_count, pattern):
    started = time.monotonic()
    failed = False
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = [
            pool.submit(run_shard_process, root, worker, worker_count, pattern)
            for worker in range(worker_count)
        ]
        for future in as_completed(futures):
            worker, result, elapsed = future.result()
            status = "passed" if result.returncode == 0 else "failed"
            print(
                f"run_tests.py: shard={worker + 1}/{worker_count} "
                f"status={status} process_elapsed_s={elapsed:.3f}",
                flush=True,
            )
            if result.stdout:
                sys.stdout.write(result.stdout)
                if not result.stdout.endswith("\n"):
                    sys.stdout.write("\n")
                sys.stdout.flush()
            failed = failed or result.returncode != 0

    elapsed = time.monotonic() - started
    status = "failed" if failed else "passed"
    print(
        f"run_tests.py: workers={worker_count} status={status} "
        f"total_elapsed_s={elapsed:.3f}",
        flush=True,
    )
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--pattern", default="test*.py")
    parser.add_argument("--worker", type=int, help=argparse.SUPPRESS)
    options = parser.parse_args(argv)
    if options.workers < 1:
        parser.error("--workers must be positive")

    root = Path(__file__).resolve().parents[1]
    if options.worker is not None:
        if not 0 <= options.worker < options.workers:
            parser.error("--worker must be between 0 and workers - 1")
        return run_worker(root, options.worker, options.workers, options.pattern)
    return run_coordinator(root, options.workers, options.pattern)


if __name__ == "__main__":
    raise SystemExit(main())
