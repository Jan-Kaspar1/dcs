#!/usr/bin/env python3
"""Run production JavaScript contract tests with Node's built-in test runner.

CI supplies Node 22 through the official actions/setup-node action. Locally,
Node 22 or newer runs directly. If Node is absent, the default runner uses the
official node:22-bookworm-slim Docker image, with a read-only repository mount
and no network during test execution. Docker may download that image first.
Use --runtime native or --runtime docker to select explicitly.

These source contract tests do not perform browser or visual review.
"""

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


DOCKER_IMAGE = "node:22-bookworm-slim"
MIN_NODE_MAJOR = 22


def native_node():
    executable = shutil.which("node")
    if executable is None:
        return None
    version = subprocess.check_output([executable, "--version"], text=True, timeout=10).strip()
    match = re.fullmatch(r"v(\d+)\.\d+\.\d+(?:[-+].*)?", version)
    return executable if match and int(match.group(1)) >= MIN_NODE_MAJOR else None


def test_command(root, tests, runtime="auto"):
    node = native_node() if runtime != "docker" else None
    if node is not None:
        return [node, "--test", *[str(path) for path in tests]], "native"
    if runtime == "native":
        raise ValueError("Node 22 or newer is required for --runtime native")
    docker = shutil.which("docker")
    if docker is None:
        raise ValueError("Node 22 or newer, or Docker with the official Node 22 image, is required")
    command = [docker, "run", "--rm", "--network", "none"]
    if os.name == "posix":
        command.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
    command.extend([
        "--mount", f"type=bind,src={root},dst=/workspace,readonly",
        "--workdir", "/workspace", DOCKER_IMAGE, "node", "--test",
        *["/workspace/" + path.relative_to(root).as_posix() for path in tests],
    ])
    return command, "docker"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", choices=("auto", "native", "docker"), default="auto")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    tests = sorted((root / "tests/ui").glob("*.test.cjs"))
    if not tests:
        parser.error("no JavaScript contract tests found in tests/ui")
    try:
        command, runtime = test_command(root, tests, args.runtime)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        parser.error(str(error))
    print(f"run_ui_tests.py: runtime={runtime} tests={len(tests)}; source contracts only, no browser review", flush=True)
    return subprocess.run(command, cwd=root, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
