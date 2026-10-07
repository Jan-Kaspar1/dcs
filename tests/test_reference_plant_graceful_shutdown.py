"""The graceful-shutdown leg's unit coverage: the LEG registration the
file-discovered stage reads, the stop-bound helpers, and the tamper
evidence contract — all without spawning the released tooling. The
leg's full run against the pinned release happens inside
reference-plant/ci/check.sh's pair stage (proven by the
consumer-upgrade and reference-plant suites); these tests pin the
pieces that can be checked cheaply: the registration shape and order,
the SIGTERM exit audit's pass and named-failure branches, and the
stall staging the second-signal probe parks in.
"""
import importlib.util
import io
import os
import stat
import sys
import threading
import time
import unittest
from pathlib import Path

_CIDIR = Path(__file__).resolve().parents[1] / "reference-plant" / "ci"
sys.path.insert(0, str(_CIDIR))
_LEG_PATH = _CIDIR / "legs" / "graceful_shutdown.py"
_spec = importlib.util.spec_from_file_location(
    "graceful_shutdown", _LEG_PATH
)
graceful_shutdown = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(graceful_shutdown)


class FakeProcess:
    """A spawned peer's stub: poll() answers None until the scripted
    exit lands, stderr.read() the staged tail."""

    def __init__(self, exits=(None, 0), tail=""):
        self._exits = list(exits)
        self.returncode = None
        self._tail = tail
        self.signals = []
        self.stderr = io.StringIO(tail)

    def poll(self):
        if self._exits:
            self.returncode = self._exits.pop(0)
        return self.returncode

    def send_signal(self, number):
        self.signals.append(number)


class RegistrationTests(unittest.TestCase):
    def test_leg_registers_with_a_free_order_and_named_tampers(self):
        leg = graceful_shutdown.LEG
        self.assertIsInstance(leg["order"], int)
        self.assertEqual(leg["order"], 165)
        for field in ("title", "passes"):
            self.assertIsInstance(leg[field], str)
        self.assertTrue(leg["tampers"])
        for tamper in leg["tampers"]:
            for field in ("name", "passed", "missed"):
                self.assertIsInstance(tamper[field], str)
            self.assertTrue(tamper["evidence"])

    def test_order_is_unique_across_the_stage(self):
        sys.path.insert(0, str(_CIDIR))
        import legs

        discovered = legs.discover(str(_CIDIR / "legs"))
        orders = [leg["order"] for leg in discovered]
        self.assertEqual(len(orders), len(set(orders)))
        names = [Path(leg["file"]).name for leg in discovered]
        self.assertIn("graceful_shutdown.py", names)

    def test_tamper_evidence_is_what_the_leg_reports(self):
        source = _LEG_PATH.read_text()
        for tamper in graceful_shutdown.LEG["tampers"]:
            for item in tamper["evidence"]:
                self.assertIn(item, source)


class StopAuditTests(unittest.TestCase):
    def test_wait_exit_returns_the_code_once_it_stops(self):
        proc = FakeProcess(exits=[None, None, 0])
        self.assertEqual(graceful_shutdown.wait_exit(proc, 5), 0)

    def test_wait_exit_times_out_on_a_hung_process(self):
        proc = FakeProcess(exits=[None] * 1000)
        started = time.monotonic()
        self.assertIsNone(graceful_shutdown.wait_exit(proc, 0.2))
        self.assertLess(time.monotonic() - started, 5)

    def test_terminate_passes_the_graceful_exit(self):
        proc = FakeProcess(
            exits=[None, 0],
            tail="graceful shutdown on SIGTERM: stopped",
        )
        failures = []
        tail = graceful_shutdown.terminate(proc, failures, "the duty")
        self.assertEqual(failures, [])
        self.assertIn("graceful shutdown", tail)
        self.assertEqual(len(proc.signals), 1)

    def test_terminate_names_a_hung_stop(self):
        proc = FakeProcess(exits=[None] * 1000, tail="")
        failures = []
        with self.assertRaises(graceful_shutdown.Abort):
            with _patched_wait():
                graceful_shutdown.terminate(proc, failures, "the duty")
        self.assertEqual(len(failures), 1)
        self.assertIn("never exited", failures[0])

    def test_terminate_names_a_killed_stop(self):
        proc = FakeProcess(exits=[None, 143], tail="terminated")
        failures = []
        with self.assertRaises(graceful_shutdown.Abort):
            graceful_shutdown.terminate(proc, failures, "the duty")
        self.assertEqual(len(failures), 1)
        self.assertIn("143", failures[0])
        self.assertIn("marker absent", failures[0])

    def test_terminate_names_a_markerless_zero_exit(self):
        proc = FakeProcess(exits=[None, 0], tail="listening on x")
        failures = []
        with self.assertRaises(graceful_shutdown.Abort):
            graceful_shutdown.terminate(proc, failures, "the duty")
        self.assertEqual(len(failures), 1)
        self.assertIn("without the graceful shutdown", failures[0])


class _patched_wait:
    """Patch wait_exit to a short bound for the hung-stop test."""

    def __enter__(self):
        self._real = graceful_shutdown.wait_exit
        graceful_shutdown.wait_exit = lambda proc, bound: None

    def __exit__(self, *args):
        graceful_shutdown.wait_exit = self._real


class StallStagingTests(unittest.TestCase):
    def test_impede_stages_a_readerless_fifo(self):
        with _scratch() as directory:
            failures = []
            graceful_shutdown.impede_sink(directory, failures)
            self.assertEqual(failures, [])
            tmp = os.path.join(directory, "state.json.tmp")
            self.assertTrue(stat.S_ISFIFO(os.lstat(tmp).st_mode))
            # Idempotent: staging twice is the same stall.
            graceful_shutdown.impede_sink(directory, failures)
            self.assertEqual(failures, [])
            os.unlink(tmp)

    def test_impede_waits_out_a_capture_in_flight(self):
        with _scratch() as directory:
            tmp = os.path.join(directory, "state.json.tmp")
            Path(tmp).write_text('{"tick": 1}')
            done = []

            def clear():
                time.sleep(0.1)
                os.replace(tmp, os.path.join(directory, "state.json"))
                done.append(True)

            thread = threading.Thread(target=clear)
            thread.start()
            failures = []
            graceful_shutdown.impede_sink(directory, failures)
            thread.join(5)
            self.assertEqual(failures, [])
            self.assertEqual(done, [True])
            self.assertTrue(
                stat.S_ISFIFO(
                    os.lstat(
                        os.path.join(directory, "state.json.tmp")
                    ).st_mode
                )
            )


class _scratch:
    def __enter__(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        return self._tmp.name

    def __exit__(self, *args):
        self._tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
