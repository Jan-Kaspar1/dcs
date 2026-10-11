"""scripts/verify.py gates: phase timing and build-slot behavior."""
import contextlib
import io
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import verify


class PhaseTimingTests(unittest.TestCase):
    def test_run_phase_logs_elapsed_time_and_preserves_command(self):
        output = io.StringIO()
        root = Path(".")
        env = {"CARGO_BUILD_JOBS": "4"}
        command = ["cargo", "test", "--workspace", "--locked"]
        with (
            patch.object(verify.time, "monotonic", side_effect=(3.0, 4.25)),
            patch.object(verify.subprocess, "run") as run,
            contextlib.redirect_stdout(output),
        ):
            elapsed = verify.run_phase("rust-tests", command, root, env)

        self.assertEqual(elapsed, 1.25)
        run.assert_called_once_with(command, cwd=root, env=env, check=True)
        self.assertIn("phase=rust-tests status=passed elapsed_s=1.250", output.getvalue())

    def test_run_phase_logs_failure_and_propagates_it(self):
        output = io.StringIO()
        error = subprocess.CalledProcessError(1, ["cargo", "clippy"])
        with (
            patch.object(verify.time, "monotonic", side_effect=(5.0, 6.5)),
            patch.object(verify.subprocess, "run", side_effect=error),
            contextlib.redirect_stdout(output),
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                verify.run_phase("rust-clippy", ["cargo", "clippy"], Path("."), {})

        self.assertIn("phase=rust-clippy status=failed elapsed_s=1.500", output.getvalue())

    def test_ci_phase_commands_match_required_checks(self):
        self.assertEqual(verify.phase_command("rust-format"), [
            "cargo", "fmt", "--all", "--", "--check",
        ])
        self.assertEqual(verify.phase_command("rust-clippy"), [
            "cargo", "clippy", "--workspace", "--all-targets", "--locked",
            "--", "-D", "warnings",
        ])
        self.assertEqual(verify.phase_command("rust-tests"), [
            verify.sys.executable, "scripts/run_rust_tests.py",
            "--scope", "workspace",
        ])
        self.assertEqual(verify.phase_command("rust-proofs"), [
            verify.sys.executable, "scripts/run_rust_tests.py",
            "--scope", "proofs",
        ])
        self.assertEqual(verify.phase_command("supervisor-tests"), [
            verify.sys.executable, "scripts/run_tests.py", "--workers", "4",
        ])
        self.assertEqual(verify.phase_command("ui-tests"), [
            verify.sys.executable, "scripts/run_ui_tests.py",
        ])

    def test_phases_cover_both_gates_and_proofs_hold_a_build_slot(self):
        self.assertEqual(
            verify.PHASES,
            (
                "rust-format",
                "supervisor-tests",
                "ui-tests",
                "rust-clippy",
                "rust-tests",
                "rust-proofs",
            ),
        )
        self.assertTrue({"rust-clippy", "rust-tests", "rust-proofs"}.issubset(verify.RUST_PHASES))
        self.assertNotIn("ui-tests", verify.RUST_PHASES)

    def test_ui_phase_runs_before_heavy_builds_without_taking_a_slot(self):
        events = []

        @contextlib.contextmanager
        def slot():
            events.append("slot-acquired")
            yield Mock(fileno=lambda: 11)

        with (
            patch.object(verify, "build_slot", slot),
            patch.object(verify, "run_phase", side_effect=lambda name, *_args, **_kwargs: events.append(name)),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            verify.main([])
        self.assertLess(events.index("ui-tests"), events.index("slot-acquired"))
        self.assertEqual(events[:3], ["rust-format", "supervisor-tests", "ui-tests"])

        with (
            patch.object(verify, "build_slot") as build_slot,
            patch.object(verify, "run_phase") as run_phase,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            verify.main(["--phase", "ui-tests"])
        build_slot.assert_not_called()
        self.assertEqual(run_phase.call_args.args[0], "ui-tests")

    def test_heavy_phase_entry_points_hold_the_shared_build_slot(self):
        # A new artifact build must not silently skip the clone-wide budget.
        # The existing lock tests own exclusion; this checks CLI routing.
        for phase in ("rust-clippy", "rust-tests", "rust-proofs", "rust-demo", "water-area-proof"):
            with self.subTest(phase=phase):
                held = False

                @contextlib.contextmanager
                def slot():
                    nonlocal held
                    held = True
                    try:
                        yield Mock(fileno=lambda: 11)
                    finally:
                        held = False

                def subprocess_run(_command, **_kwargs):
                    self.assertTrue(held, f"{phase} started outside the shared build slot")

                with (
                    patch.object(verify, "build_slot", slot),
                    patch.object(verify.subprocess, "run", side_effect=subprocess_run) as run,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    verify.main(["--phase", phase])
                run.assert_called_once()
                self.assertFalse(held)


class CiWorkflowGateTests(unittest.TestCase):
    """ci.yml must carry every verify phase as a job, keep the required
    set on the fast legs, and run the assembly gate on PRs, main, and
    release tags with the history its proofs resolve against."""

    WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"

    @classmethod
    def setUpClass(cls):
        cls.text = cls.WORKFLOW.read_text()

    def job_names(self):
        body = self.text.split("\njobs:\n", 1)[1]
        return re.findall(r"^  ([a-z0-9-]+):$", body, re.M)

    def job_body(self, name):
        match = re.search(
            rf"^  {re.escape(name)}:\n(.*?)(?=^  [a-z0-9-]+:$|\Z)",
            self.text, re.M | re.S)
        self.assertIsNotNone(match, f"no workflow job named {name}")
        return match.group(1)

    def test_every_verify_phase_is_a_workflow_job(self):
        jobs = self.job_names()
        for phase in verify.PHASES:
            self.assertIn(phase, jobs)

    def test_each_job_runs_its_named_verify_phase(self):
        for name in self.job_names():
            self.assertIn(f"verify.py --phase {name}", self.job_body(name))

    def test_required_checks_are_the_fast_legs_and_proofs_are_separate(self):
        from agent_pool.config import DEFAULT_CHECKS

        for check in DEFAULT_CHECKS:
            self.assertIn(check, verify.PHASES)
        self.assertNotIn("rust-proofs", DEFAULT_CHECKS)
        self.assertIn("rust-proofs", self.job_names())

    def test_assembly_gate_triggers_cover_pr_main_and_release_tags(self):
        match = re.search(r"^on:\n((?: {2}[^\n]*\n)+)", self.text, re.M)
        self.assertIsNotNone(match)
        triggers = match.group(1)
        self.assertIn("pull_request:", triggers)
        self.assertIn("branches: [main]", triggers)
        self.assertIn('"v*"', triggers)
        self.assertIn("workflow_dispatch:", triggers)

    def test_proofs_job_checks_out_full_history_and_reports_metrics(self):
        # The repin proofs resolve the recorded release rev out of git
        # history; a shallow checkout would silently degrade them.
        body = self.job_body("rust-proofs")
        self.assertIn("verify.py --phase rust-proofs", body)
        self.assertIn("fetch-depth: 0", body)

    def test_cached_rust_jobs_report_feedback_metrics(self):
        for name in ("rust-clippy", "rust-tests", "rust-proofs"):
            body = self.job_body(name)
            self.assertIn("Report feedback metrics", body)
            self.assertIn("cache-hit", body)

    def test_ui_job_installs_node_22_and_runs_the_production_contract_gate(self):
        body = self.job_body("ui-tests")
        self.assertIn("actions/setup-node@", body)
        self.assertIn('node-version: "22"', body)
        self.assertIn("verify.py --phase ui-tests", body)


class UiTestRunnerTests(unittest.TestCase):
    def test_native_node_22_runs_the_builtin_test_runner(self):
        from scripts import run_ui_tests

        root = Path("/workspace")
        tests = [root / "tests/ui/wire-json.test.cjs"]
        with patch.object(run_ui_tests, "native_node", return_value="/usr/bin/node"):
            command, runtime = run_ui_tests.test_command(root, tests)
        self.assertEqual(runtime, "native")
        self.assertEqual(command, ["/usr/bin/node", "--test", str(tests[0])])

    def test_missing_native_node_uses_official_isolated_docker_image(self):
        from scripts import run_ui_tests

        root = Path("/workspace")
        tests = [root / "tests/ui/wire-json.test.cjs"]
        with (
            patch.object(run_ui_tests, "native_node", return_value=None),
            patch.object(run_ui_tests.shutil, "which", return_value="/usr/bin/docker"),
        ):
            command, runtime = run_ui_tests.test_command(root, tests)
        self.assertEqual(runtime, "docker")
        self.assertIn("node:22-bookworm-slim", command)
        self.assertIn("type=bind,src=/workspace,dst=/workspace,readonly", command)
        self.assertEqual(command[command.index("--network") + 1], "none")
        self.assertEqual(command[-3:], ["node", "--test", "/workspace/tests/ui/wire-json.test.cjs"])

    def test_node_before_22_is_not_silently_used(self):
        from scripts import run_ui_tests

        with (
            patch.object(run_ui_tests.shutil, "which", return_value="/usr/bin/node"),
            patch.object(run_ui_tests.subprocess, "check_output", return_value="v20.19.0\n"),
        ):
            self.assertIsNone(run_ui_tests.native_node())
        with (
            patch.object(run_ui_tests, "native_node", return_value=None),
            patch.object(run_ui_tests.shutil, "which", return_value=None),
        ):
            with self.assertRaisesRegex(ValueError, "Node 22"):
                run_ui_tests.test_command(Path("/workspace"), [], "native")

    def test_missing_both_runtimes_is_a_failure_not_a_skipped_gate(self):
        from scripts import run_ui_tests

        with (
            patch.object(run_ui_tests, "native_node", return_value=None),
            patch.object(run_ui_tests.shutil, "which", return_value=None),
        ):
            with self.assertRaisesRegex(ValueError, "Node 22 or newer, or Docker"):
                run_ui_tests.test_command(Path("/workspace"), [])


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
                with (
                    patch('os.name', 'nt'),
                    patch.dict(sys.modules, {'msvcrt': fake}),
                ):
                    spec.loader.exec_module(mod)
                    self.assertTrue(mod._try_lock(held))
                    self.assertFalse(mod._try_lock(rival))
                    mod._release(held)
                    self.assertTrue(mod._try_lock(reacquired))
            finally:
                held.close()
                rival.close()
                reacquired.close()


if __name__ == "__main__":
    unittest.main()
