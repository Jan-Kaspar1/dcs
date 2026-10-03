"""Installer preflight: gate selection, refusal ordering, and pointer safety."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import install_agents, verify

REVISION = "9f8e7d6c5b4a"


class Harness:
    """Fake subprocess layer: systemd, git, and the CI gate."""

    def __init__(self, *, active=False, dirty=False, gate_fails=False):
        self.active = active
        self.dirty = dirty
        self.gate_fails = gate_fails
        self.runs = []

    def run(self, args, **kwargs):
        self.runs.append(list(args))
        if args[0] == "systemctl":
            code = 0
            if "is-active" in args:
                code = 0 if self.active else 3
            return subprocess.CompletedProcess(args, code)
        if "verify.py" in str(args):
            if self.gate_fails:
                raise subprocess.CalledProcessError(1, args)
            return subprocess.CompletedProcess(args, 0)
        return subprocess.CompletedProcess(args, 0)

    def check_output(self, args, **kwargs):
        if "rev-parse" in args:
            return REVISION + "\n"
        return " M scripts/install_agents.py\n" if self.dirty else ""

    def __enter__(self):
        self._patches = [
            patch("subprocess.run", side_effect=self.run),
            patch("subprocess.check_output", side_effect=self.check_output),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()

    def gate_calls(self):
        return [call for call in self.runs if "verify.py" in str(call)]


def make_source(root):
    for directory in ("agent_pool", "qa_lane", "scripts"):
        path = Path(root) / directory
        path.mkdir(parents=True)
        (path / "marker.txt").write_text(directory)
    return Path(root)


class PreflightCommandTests(unittest.TestCase):
    def test_preflight_delegates_to_the_ci_supervisor_gate(self):
        command = install_agents.preflight_command()
        self.assertEqual(command[1:], ["scripts/verify.py", "--phase", "supervisor-tests"])
        # The named phase is the complete sharded Python gate CI runs.
        self.assertEqual(
            verify.phase_command("supervisor-tests"),
            [verify.sys.executable, "scripts/run_tests.py", "--workers", "4"],
        )


class RefusalTests(unittest.TestCase):
    def test_active_service_refuses_before_git_or_gate(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as src:
            with Harness(active=True) as harness:
                with self.assertRaises(SystemExit):
                    install_agents.main(source=make_source(src), home=home)
            self.assertEqual(len(harness.runs), 1)
            self.assertFalse((Path(home) / ".local").exists())

    def test_dirty_checkout_refuses_before_the_gate(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as src:
            with Harness(dirty=True) as harness:
                with self.assertRaises(SystemExit):
                    install_agents.main(source=make_source(src), home=home)
            self.assertEqual(harness.gate_calls(), [])
            self.assertFalse((Path(home) / ".local").exists())

    def test_failed_shard_never_moves_the_installed_pointer(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as src:
            base = Path(home) / ".local/share/dcs-agents"
            previous = base / "releases" / "previous-revision"
            previous.mkdir(parents=True)
            (base / "current").symlink_to(previous)

            with Harness(gate_fails=True) as harness:
                with self.assertRaises(subprocess.CalledProcessError):
                    install_agents.main(source=make_source(src), home=home)

            self.assertEqual(len(harness.gate_calls()), 1)
            self.assertEqual((base / "current").resolve(), previous)
            self.assertFalse((base / "releases" / REVISION).exists())


class InstallTests(unittest.TestCase):
    def test_passed_gate_installs_release_and_switches_pointer(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as src:
            with Harness() as harness:
                install_agents.main(source=make_source(src), home=home)

            self.assertEqual(len(harness.gate_calls()), 1)
            base = Path(home) / ".local/share/dcs-agents"
            release = base / "releases" / REVISION
            self.assertEqual((release / "REVISION").read_text().strip(), REVISION)
            for directory in ("agent_pool", "qa_lane", "scripts"):
                self.assertEqual((release / directory / "marker.txt").read_text(), directory)
            self.assertEqual((base / "current").resolve(), release)
            self.assertFalse((base / "current.new").exists())

            launcher = Path(home) / ".local/bin/dcs-agents"
            self.assertTrue(os.access(launcher, os.X_OK))
            self.assertIn("-m agent_pool", launcher.read_text())

            config = Path(home) / ".config/dcs-agents/config.json"
            self.assertEqual(oct(config.stat().st_mode & 0o777), "0o600")
            self.assertEqual(json.loads(config.read_text())["state_root"], str(base / "state"))
            self.assertTrue((Path(home) / ".config/systemd/user/dcs-agents.service").exists())
            self.assertEqual(harness.runs[-1][:3], ["systemctl", "--user", "daemon-reload"])

    def test_existing_config_is_preserved(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as src:
            config = Path(home) / ".config/dcs-agents/config.json"
            config.parent.mkdir(parents=True)
            config.write_text('{"custom": true}\n')
            with Harness():
                install_agents.main(source=make_source(src), home=home)
            self.assertEqual(config.read_text(), '{"custom": true}\n')


if __name__ == "__main__":
    unittest.main()
