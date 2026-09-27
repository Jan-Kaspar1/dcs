"""The orchestrated_restart leg's unit coverage — ci/legs/
orchestrated_restart.py is the reference plant's consumer-boundary
exercise of the container health contract's orchestrated-restart
clause: the manifest-declared pair rolled one container at a time,
each restart's wait sequenced on the declared `GET /health` liveness
answer through the shipped `dcs-ctl health` probe — `unhealthy`
until the answer serves, `healthy` once — never a fixed sleep, the
field-owning peer scanning and serving uninterrupted, the pair
ending at one active plus one tracking standby with launch roles
restored. These tests pin, without launching the pair: the leg's
registration record, its probe and liveness classifications, the
slow-starting stand-in the harness admits, and the inconclusive /
doctored-case classifications the harness relies on."""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock
import urllib.error

_ROOT = Path(__file__).resolve().parents[1]
_CI_DIR = _ROOT / "reference-plant" / "ci"
_LEG_PATH = _CI_DIR / "legs" / "orchestrated_restart.py"
_SCENARIO_PATH = _CI_DIR / "scenario.json"
_MANIFEST_PATH = _ROOT / "reference-plant" / "deploy" / "manifest.json"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


legs = load(_CI_DIR / "legs.py", "legs")
leg = load(_LEG_PATH, "orchestrated_restart")


def argv(tamper=None):
    args = [
        "orchestrated_restart.py",
        "--ctl", "/nonexistent/dcs-ctl",
        "--plant-server", "/nonexistent/dcs-plant-server",
        "--controller", "/nonexistent/dcs-controller",
        "--model", "/nonexistent/plant.json",
        "--dynamics", "/nonexistent/dynamics.json",
        "--scenario", str(_SCENARIO_PATH),
        "--manifest", str(_MANIFEST_PATH),
    ]
    if tamper is not None:
        args += ["--tamper", tamper]
    return args


def run_main(tamper=None, outcome=None):
    """`main()` against a stubbed pass — `outcome` the return value
    or the exception orchestrated_restart_pass raises. Returns
    `(rc, stdout, stderr)`."""
    if isinstance(outcome, BaseException):
        stub = mock.Mock(side_effect=outcome)
    else:
        stub = mock.Mock(return_value=outcome)
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", argv(tamper)), \
            mock.patch.object(leg, "orchestrated_restart_pass", stub), \
            contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        rc = leg.main()
    return rc, out.getvalue(), err.getvalue()


def probe_script(directory, line):
    """A stand-in `dcs-ctl` exiting `line`'s status — the probe the
    declared HEALTHCHECK runs."""
    path = Path(directory) / "dcs-ctl"
    path.write_text(f"#!/bin/sh\nexit {line}\n")
    path.chmod(0o755)
    return str(path)


class Registration(unittest.TestCase):
    """The leg's `LEG` literal — the pair stage's discovery contract:
    the declared order is unique across the directory, the stem and
    the named diagnostics follow the file-name convention, the shipped
    probe resolves through the declared tool flag, and the doctored
    case declares the evidence the honest run reports."""

    def test_the_leg_registers_in_the_pair_stage(self):
        discovered = {
            Path(record["file"]).name: record
            for record in legs.discover(str(_CI_DIR / "legs"))
        }
        record = discovered["orchestrated_restart.py"]
        self.assertEqual(record["stem"], "orchestrated-restart")
        self.assertEqual(record["order"], 430)

    def test_the_named_diagnostics_follow_the_stem_convention(self):
        # The issue names orchestrated-restart-failed and
        # orchestrated-restart-nondeterministic — the driver's
        # defaults off the file stem, undeclared in the literal.
        self.assertNotIn("failed", leg.LEG)
        self.assertEqual(leg.LEG["passes"], "orchestrated-restart-leg")

    def test_the_shipped_probe_resolves_through_the_tools_flag(self):
        # `dcs-ctl <addr> health` is the probe the image's declared
        # HEALTHCHECK runs — the stage resolves it under --tools.
        self.assertEqual(leg.LEG["tools"], {"ctl": "dcs-ctl"})

    def test_the_doctored_case_carries_named_evidence(self):
        tampers = {entry["name"]: entry for entry in leg.LEG["tampers"]}
        self.assertEqual(set(tampers), {"premature-healthy"})
        # The declared evidence must be the diagnostic the leg
        # actually prints — pin it against the source's failure
        # lines so a drifted message cannot pass the harness's
        # substring check by accident.
        source = _LEG_PATH.read_text()
        for name, record in tampers.items():
            self.assertTrue(record["evidence"], name)
            for evidence in record["evidence"]:
                self.assertIn(evidence, source, name)
            for field in ("passed", "missed"):
                self.assertIsInstance(record[field], str)


class ProbeClassification(unittest.TestCase):
    """The declared health status the orchestrated wait reads — the
    probe's exit zero is `healthy`, every other answer `unhealthy`,
    and the doctored probe declares healthy before the liveness
    answer serves — plus the raw `GET /health` answer's pre-contract
    classification the inconclusive verdict rides on."""

    def args(self, ctl):
        return types.SimpleNamespace(ctl=ctl)

    def test_a_zero_probe_exit_is_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            ctl = probe_script(directory, 0)
            self.assertEqual(
                leg.health_status(
                    self.args(ctl), "http://127.0.0.1:1"
                ),
                "healthy",
            )

    def test_a_nonzero_probe_exit_is_unhealthy(self):
        with tempfile.TemporaryDirectory() as directory:
            ctl = probe_script(directory, 1)
            self.assertEqual(
                leg.health_status(
                    self.args(ctl), "http://127.0.0.1:1"
                ),
                "unhealthy",
            )

    def test_a_missing_probe_binary_is_unhealthy(self):
        self.assertEqual(
            leg.health_status(
                self.args("/nonexistent/dcs-ctl"), "http://127.0.0.1:1"
            ),
            "unhealthy",
        )

    def test_the_doctored_probe_declares_healthy_early(self):
        with tempfile.TemporaryDirectory() as directory:
            ctl = probe_script(directory, 1)
            self.assertEqual(
                leg.health_status(
                    self.args(ctl),
                    "http://127.0.0.1:1",
                    tamper="premature-healthy",
                ),
                "healthy",
            )

    def test_the_liveness_answer_decodes_a_serving_peer(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps(
                    {"live": True, "role": "active", "tick": 7,
                     "last_scan_age_ms": 12}
                ).encode()

        with mock.patch.object(
            leg.urllib.request, "urlopen", return_value=Response()
        ):
            status, body = leg.liveness("http://127.0.0.1:9")
        self.assertEqual(status, 200)
        self.assertEqual(body["live"], True)
        self.assertEqual(body["role"], "active")

    def test_a_missing_endpoint_classifies_pre_contract(self):
        http_error = urllib.error.HTTPError(
            "http://127.0.0.1:9/health", 404, "not found", None,
            io.BytesIO(b""),
        )
        with mock.patch.object(
            leg.urllib.request, "urlopen", side_effect=http_error
        ):
            status, body = leg.liveness("http://127.0.0.1:9")
        self.assertEqual(status, 404)
        self.assertIsNone(body)

    def test_a_refused_connect_is_a_probe_unhealthy(self):
        with mock.patch.object(
            leg.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("refused"),
        ):
            status, body = leg.liveness("http://127.0.0.1:9")
        self.assertIsNone(status)


class SlowStandin(unittest.TestCase):
    """The slow-starting stand-in the harness admits — a wrapper
    holding the container's boot before the released binary execs,
    so the declared status transitions unhealthy→healthy rather
    than a fixed sleep passing it."""

    def test_the_standin_delays_then_execs_the_controller(self):
        with tempfile.TemporaryDirectory() as directory:
            path = leg.slow_standin(directory, "/tools/dcs-controller")
            self.assertTrue(os.access(path, os.X_OK))
            content = Path(path).read_text()
            self.assertIn(f"sleep {leg.STANDIN_DELAY_S}", content)
            self.assertIn('exec /tools/dcs-controller "$@"', content)

    def test_the_standin_quotes_a_spaced_controller_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = leg.slow_standin(directory, "/tools dir/dcs-controller")
            content = Path(path).read_text()
            self.assertIn("exec '/tools dir/dcs-controller'", content)

    def test_a_scratch_that_cannot_carry_the_standin_is_inconclusive(self):
        with self.assertRaises(leg.Inconclusive):
            leg.slow_standin("/nonexistent/never", "/tools/dcs-controller")


class Inconclusive(unittest.TestCase):
    """The release-precedence classification: a pinned release
    predating the health contract raises the leg's Inconclusive,
    which main renders as a stable digest line and a zero exit —
    and a doctored case over an inconclusive run still fails, since
    it can name no evidence."""

    def test_main_reports_the_inconclusive_digest(self):
        rc, out, err = run_main(
            outcome=leg.Inconclusive(
                "the pinned release predates the health contract"
            )
        )
        self.assertEqual(rc, 0)
        self.assertIn(
            "orchestrated-restart-digest inconclusive — the pinned "
            "release predates",
            out,
        )
        self.assertIn("orchestrated-restart: inconclusive", err)

    def test_two_inconclusive_passes_render_identically(self):
        first = run_main(outcome=leg.Inconclusive("pre-contract"))
        second = run_main(outcome=leg.Inconclusive("pre-contract"))
        self.assertEqual(first[0], 0)
        self.assertEqual(first[1], second[1])

    def test_a_doctored_case_over_an_inconclusive_run_fails(self):
        rc, out, err = run_main(
            tamper="premature-healthy",
            outcome=leg.Inconclusive("pre-contract"),
        )
        self.assertEqual(rc, 1)
        self.assertIn(
            "the doctored probe declared the restarted peer healthy "
            "before its liveness answer served",
            err,
        )


class Classification(unittest.TestCase):
    """main()'s exit classification over the pass result — failures
    stream to stderr prefixed by the stem, a doctored case may never
    exit zero, and a clean pass renders the sha digest line."""

    def test_failures_report_by_name(self):
        rc, out, err = run_main(
            outcome=([], {}, ["the restarted peer never reported healthy"])
        )
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn(
            "orchestrated-restart: the restarted peer never reported "
            "healthy",
            err,
        )

    def test_an_abort_reports_by_name(self):
        rc, out, err = run_main(
            outcome=leg.Abort("the pair never converged")
        )
        self.assertEqual(rc, 1)
        self.assertIn("orchestrated-restart: the pair never converged", err)

    def test_a_doctored_case_never_passes_silently(self):
        rc, out, err = run_main(
            tamper="premature-healthy", outcome=([], {}, [])
        )
        self.assertEqual(rc, 1)
        self.assertIn("passed silently", err)

    def test_a_doctored_case_carrying_failures_still_fails(self):
        rc, _, _ = run_main(
            tamper="premature-healthy",
            outcome=([], {}, ["the honest run reconverged"]),
        )
        self.assertEqual(rc, 1)

    def test_a_clean_pass_renders_the_digest_line(self):
        evidence = {
            "converged": 4,
            "switched_at": 8,
            "restored_at": 16,
            "final_tick": 21,
        }
        rc, out, err = run_main(
            outcome=([{"phase": "converge"}], evidence, [])
        )
        self.assertEqual(rc, 0)
        self.assertRegex(
            out, r"^orchestrated-restart-digest [0-9a-f]{64} — "
        )
        self.assertIn("tracking by tick 4", out)
        self.assertIn("switched at tick 8", out)
        self.assertIn("restored at tick 16", out)
        self.assertIn("tick 21", out)


if __name__ == "__main__":
    unittest.main()
