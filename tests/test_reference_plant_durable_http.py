"""A stalled durable-history scan has an unknown outcome, never a retry.

The old unbounded client hid a panicked persistence writer indefinitely.
Timeouts must fail the acceptance leg without duplicating possibly applied
scans, while the existing named dropped-request retry stays available.
"""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
import urllib.error

_PATH = Path(__file__).resolve().parents[1] / "reference-plant/ci/legs/durable_history.py"
_SPEC = importlib.util.spec_from_file_location("durable_history_http_test", _PATH)
leg = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(leg)


class UnknownScanOutcome(unittest.TestCase):
    def test_connect_and_response_timeouts_fail_without_resubmitting(self):
        for error in [TimeoutError("response"), urllib.error.URLError(TimeoutError("connect"))]:
            with self.subTest(error=error), patch.object(
                leg.urllib.request, "urlopen", side_effect=error
            ) as request:
                failures = []
                with self.assertRaises(leg.Abort):
                    leg.scan_n("http://fixture", 32, failures)
                self.assertEqual(request.call_count, 1)
                self.assertEqual(request.call_args.kwargs["timeout"], leg.HTTP_TIMEOUT)
                self.assertIn("outcome unknown, not resubmitted", failures[0])

    def test_history_read_timeout_also_ends_the_retry_window(self):
        with patch.object(leg.urllib.request, "urlopen", side_effect=TimeoutError("read")) as request:
            with self.assertRaisesRegex(RuntimeError, "not resubmitted"):
                leg.resilient_http("http://fixture/history/durable")
            self.assertEqual(request.call_count, 1)

    def test_a_refused_connection_keeps_the_existing_retry(self):
        with patch.object(
            leg, "bounded_http", side_effect=[ConnectionRefusedError(), {"tick": 32}]
        ) as request, patch.object(leg.time, "sleep"):
            self.assertEqual(leg.scan_n("http://fixture", 32, []), {"tick": 32})
            self.assertEqual(request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
