"""The responsiveness leg's unit coverage — ci/legs/responsiveness.py
is the reference plant's consumer-boundary mirror of the qa rig's
bounded-responsiveness leg (#583, landed through #634): the manifest-
declared pair's monitor must keep answering while a peer waits on an
unreachable network, because a consumer-facing UI that stalls only when
a peer dies is exactly the WW-FND-004 dishonesty the publication
boundary exists to prevent. These tests pin, without launching the
pair: the leg's registration record, the declared bounds and the
sampling verdict its digest carries, the freeze/thaw lever the stage's
existing stop/pause convention gives it, the receipted-command
classification, and the inconclusive / doctored-case classifications
`main` renders."""
import contextlib
import importlib.util
import io
import json
import signal
import sys
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
_CI_DIR = _ROOT / "reference-plant" / "ci"
_LEG_PATH = _CI_DIR / "legs" / "responsiveness.py"
_SCENARIO_PATH = _CI_DIR / "scenario.json"
_MODEL_PATH = _ROOT / "reference-plant" / "model" / "plant.json"
_MANIFEST_PATH = _ROOT / "reference-plant" / "deploy" / "manifest.json"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


legs = load(_CI_DIR / "legs.py", "legs")
leg = load(_LEG_PATH, "responsiveness")
model = json.loads(_MODEL_PATH.read_text())


def argv(tamper=None):
    args = [
        "responsiveness.py",
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
    """`main()` against a stubbed pass — `outcome` the return value or
    the exception responsiveness_pass raises. Returns `(rc, stdout,
    stderr)`."""
    if isinstance(outcome, BaseException):
        stub = mock.Mock(side_effect=outcome)
    else:
        stub = mock.Mock(return_value=outcome)
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", argv(tamper)), \
            mock.patch.object(leg, "responsiveness_pass", stub), \
            contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        rc = leg.main()
    return rc, out.getvalue(), err.getvalue()


class Registration(unittest.TestCase):
    """The leg's `LEG` literal — the pair stage's discovery contract:
    the declared order is free and unique in the directory, the stem
    and the named diagnostics follow the file-name convention, and the
    doctored case declares the evidence the honest run reports."""

    def test_the_leg_registers_in_the_pair_stage(self):
        discovered = legs.discover(str(_CI_DIR / "legs"))
        record = [
            leg for leg in discovered if leg["stem"] == "responsiveness"
        ]
        self.assertEqual(len(record), 1)
        # The slot the leg declared, past every order already taken.
        self.assertEqual(record[0]["order"], 860)
        orders = [leg["order"] for leg in discovered]
        self.assertEqual(len(orders), len(set(orders)))

    def test_the_named_diagnostics_follow_the_stem_convention(self):
        # The issue names responsiveness-failed and
        # responsiveness-nondeterministic — the driver's defaults off
        # the file stem, undeclared in the literal.
        self.assertNotIn("failed", leg.LEG)
        self.assertEqual(leg.LEG["passes"], "responsiveness-leg")

    def test_the_doctored_case_carries_named_evidence(self):
        tampers = {case["name"]: case for case in leg.LEG["tampers"]}
        self.assertEqual(set(tampers), {"starved-endpoints"})
        source = _LEG_PATH.read_text()
        for name, case in tampers.items():
            self.assertTrue(case["evidence"], name)
            for evidence in case["evidence"]:
                self.assertIn(evidence, source, name)
            for field in ("passed", "missed"):
                self.assertIsInstance(case[field], str)

    def test_the_contract_declares_the_emitted_diagnostics(self):
        contract = (_ROOT / "docs" / "release-contract.md").read_text()
        for name in (
            "`responsiveness-failed`",
            "`responsiveness-nondeterministic`",
            "`responsiveness-unchecked`",
        ):
            self.assertIn(name, contract)


class Bounds(unittest.TestCase):
    """The declared bounds the leg records. The responsiveness claim is
    only meaningful against the pre-fix blocking window it sits under:
    the batch's own occupancy — `BATCH_SCANS` pulls at the monitor's
    documented per-fetch bound — is what a single worker used to serve
    every request behind, so the sampled bound must be far inside it
    while still above what the healthy pair owes."""

    def test_the_sampled_bound_sits_inside_the_pre_fix_window(self):
        blocking_window = leg.BATCH_SCANS * leg.PULL_BOUND
        self.assertGreater(blocking_window, leg.RESPONSIVE_BOUND)
        self.assertGreater(leg.RESPONSIVE_BOUND, leg.PULL_BOUND)

    def test_the_pull_bound_is_the_monitors_documented_fetch_bound(self):
        # The controller crate's one-second CHECKPOINT_PULL_TIMEOUT,
        # restated at the consumer boundary.
        self.assertEqual(leg.PULL_BOUND, 1.0)

    def test_the_batch_sizes_are_positive_and_bounded(self):
        for bound in (
            leg.BATCH_SCANS,
            leg.HEALTHY_BATCH_SCANS,
            leg.WINDOW,
            leg.POLL,
            leg.MIN_ROUNDS,
            leg.RESTORE_TICKS,
        ):
            self.assertGreater(bound, 0)
        # The healthy batch is the large one, and both stay under the
        # monitor's declared per-request scan bound.
        self.assertGreater(leg.HEALTHY_BATCH_SCANS, leg.BATCH_SCANS)
        self.assertLessEqual(leg.HEALTHY_BATCH_SCANS, 256)

    def test_the_sampled_endpoints_span_two_serving_lanes(self):
        # `/role` rides the heartbeat lane, `/snapshot` and `/journal`
        # the serving lane — the rotation covers one endpoint per lane.
        self.assertEqual(
            leg.SAMPLE_PATHS, ("/snapshot", "/role", "/journal")
        )
        self.assertIn("/role", leg.SAMPLE_PATHS)
        self.assertIn("/journal", leg.SAMPLE_PATHS)

    def test_the_deadlines_cover_their_batches(self):
        self.assertGreaterEqual(
            leg.BATCH_DEADLINE, leg.BATCH_SCANS * leg.PULL_BOUND
        )
        self.assertGreaterEqual(
            leg.HEALTHY_BATCH_DEADLINE,
            leg.HEALTHY_BATCH_SCANS * leg.PULL_BOUND,
        )


class Sampling(unittest.TestCase):
    """The window's sampling verdict — the digest's per-endpoint word
    is `bounded` only when every sample landed inside the declared
    bound, and a lane refusal names the request queueing the split
    removes."""

    def test_every_sample_inside_the_bound_reads_bounded(self):
        counts = {
            path: {"samples": 3, "within": 3}
            for path in leg.SAMPLE_PATHS
        }
        self.assertEqual(
            leg._served(counts),
            {path: "bounded" for path in leg.SAMPLE_PATHS},
        )

    def test_a_late_sample_reads_starved(self):
        counts = {path: {"samples": 3, "within": 3} for path in leg.SAMPLE_PATHS}
        counts["/journal"] = {"samples": 3, "within": 1}
        self.assertEqual(leg._served(counts)["/journal"], "starved")

    def test_an_unsampled_endpoint_reads_starved(self):
        counts = {
            path: {"samples": 0, "within": 0} for path in leg.SAMPLE_PATHS
        }
        self.assertEqual(
            set(leg._served(counts).values()), {"starved"}
        )

    def test_a_bounded_read_reports_its_elapsed_time(self):
        # A bounded read names how long it took; an unreadable one
        # reports the error instead — the leg's whole claim rides which
        # of the two came back.
        with mock.patch.object(
            leg.urllib.request, "urlopen",
            return_value=io.BytesIO(b'{"tick": 4}'),
        ) as opener:
            body, elapsed = leg.sampled(
                "http://127.0.0.1:1", "/snapshot", leg.RESPONSIVE_BOUND
            )
        self.assertEqual(body, {"tick": 4})
        self.assertIsInstance(elapsed, float)
        self.assertLessEqual(elapsed, leg.RESPONSIVE_BOUND)
        self.assertEqual(
            opener.call_args.args[0], "http://127.0.0.1:1/snapshot"
        )
        self.assertEqual(
            opener.call_args.kwargs["timeout"], leg.RESPONSIVE_BOUND
        )
        with mock.patch.object(
            leg.urllib.request, "urlopen", side_effect=OSError("timed out")
        ):
            body, error = leg.sampled(
                "http://127.0.0.1:1", "/snapshot", leg.RESPONSIVE_BOUND
            )
        self.assertIsNone(body)
        self.assertIsInstance(error, OSError)


class VerdictWords(unittest.TestCase):
    """The receipted command's classification — an admitted receipt,
    a named refusal, and the command lane's `queue_full` refusal the
    leg names as the queueing the split removes."""

    def test_an_admitted_receipt_reads_its_verdict(self):
        self.assertEqual(
            leg.receipt_outcome({"outcome": {"accepted": {}}}), "accepted"
        )
        self.assertEqual(
            leg.receipt_outcome({"outcome": {"applied": {}}}), "applied"
        )

    def test_a_refused_receipt_reads_its_reason(self):
        self.assertEqual(
            leg.receipt_outcome(
                {"outcome": {"rejected": {"reason": {"not_active": {}}}}}
            ),
            "not_active",
        )

    def test_the_write_target_is_a_writable_boolean_input(self):
        point = leg.write_command(model)
        declared = {entry["id"]: entry for entry in model["io_points"]}
        self.assertIn(point, declared)
        self.assertTrue(declared[point].get("writable"))
        self.assertEqual(declared[point]["direction"], "in")
        self.assertEqual(declared[point]["value_type"], "bool")
        self.assertIsNone(
            leg.write_command(
                {"io_points": [{"id": 1, "writable": False}]}
            )
        )


class Levers(unittest.TestCase):
    """The freeze/thaw pair the stage's existing stop/pause convention
    gives the leg: `SIGSTOP` holds the checkpoint source unreachable
    with its socket still open — each pull waits the documented bound
    instead of failing fast — and `SIGCONT` releases it."""

    def test_the_freeze_holds_the_source_and_the_thaw_releases_it(self):
        process = mock.Mock()
        leg.freeze_source(process)
        process.send_signal.assert_called_once_with(signal.SIGSTOP)
        leg.thaw_source(process)
        process.send_signal.assert_called_with(signal.SIGCONT)

    def test_a_host_without_the_lever_is_inconclusive(self):
        with mock.patch.object(leg.signal, "SIGSTOP", None, create=True):
            with mock.patch.object(leg, "hasattr", create=True,
                                   return_value=False):
                with self.assertRaises(leg.Inconclusive) as caught:
                    leg.freeze_source(mock.Mock())
        self.assertIn("stop/pause lever", str(caught.exception))

    def test_a_lever_that_never_lands_is_inconclusive(self):
        process = mock.Mock()
        process.send_signal.side_effect = OSError("no such process")
        with self.assertRaises(leg.Inconclusive) as caught:
            leg.freeze_source(process)
        self.assertIn("never landed", str(caught.exception))

    def test_a_thaw_that_cannot_land_never_raises(self):
        # The restore runs on the failure and teardown paths too — a
        # source the harness already lost must not mask the verdict.
        process = mock.Mock()
        process.send_signal.side_effect = OSError("gone")
        leg.thaw_source(process)


class Inconclusive(unittest.TestCase):
    """The release-precedence classification: a harness admitting no
    freeze lever, or a pinned release predating the served surface the
    leg samples, raises the leg's Inconclusive, which main renders as a
    stable digest line and a zero exit — and a doctored case over an
    inconclusive run still fails, since it can name no evidence."""

    def test_main_reports_the_inconclusive_digest(self):
        rc, out, err = run_main(
            outcome=leg.Inconclusive(
                "the consumer harness admits no stop/pause lever — no "
                "SIGSTOP to hold the checkpoint source unreachable"
            )
        )
        self.assertEqual(rc, 0)
        self.assertIn("responsiveness-digest inconclusive", out)
        self.assertIn("stop/pause lever", out)
        self.assertIn("responsiveness: inconclusive —", err)

    def test_two_inconclusive_passes_render_identically(self):
        first = run_main(outcome=leg.Inconclusive("pre-contract"))
        second = run_main(outcome=leg.Inconclusive("pre-contract"))
        self.assertEqual(first[0], 0)
        self.assertEqual(first[1], second[1])

    def test_a_doctored_case_over_an_inconclusive_run_fails(self):
        rc, _, err = run_main(
            tamper="starved-endpoints",
            outcome=leg.Inconclusive("pre-contract"),
        )
        self.assertEqual(rc, 1)
        self.assertIn(
            "the doctored expectation wanted the sampled reads to "
            "starve behind the dead peer's pulls",
            err,
        )


class Classification(unittest.TestCase):
    """main()'s exit classification over the pass result — failures
    stream to stderr prefixed by the stem, a doctored case may never
    exit zero, and a clean pass renders the sha digest line."""

    def test_failures_report_by_name(self):
        rc, out, err = run_main(
            outcome=([], {}, ["GET /journal never answered inside the "
                              "declared 2.0s bound"])
        )
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("responsiveness: GET /journal never answered", err)

    def test_an_abort_reports_by_name(self):
        rc, out, err = run_main(
            outcome=leg.Abort("the pair never converged")
        )
        self.assertEqual(rc, 1)
        self.assertIn("responsiveness: the pair never converged", err)

    def test_a_doctored_case_never_passes_silently(self):
        rc, out, err = run_main(
            tamper="starved-endpoints", outcome=([], {}, [])
        )
        self.assertEqual(rc, 1)
        self.assertIn("passed silently", err)

    def test_a_doctored_case_carrying_failures_still_fails(self):
        rc, _, err = run_main(
            tamper="starved-endpoints",
            outcome=([], {}, [leg.TAMPER_EVIDENCE]),
        )
        self.assertEqual(rc, 1)
        self.assertIn(leg.TAMPER_EVIDENCE, err)

    def test_a_clean_pass_renders_the_digest_line(self):
        evidence = {"converged": 4, "restored": 8}
        rc, out, err = run_main(
            outcome=([{"phase": "converge"}], evidence, [])
        )
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"^responsiveness-digest [0-9a-f]{64} — ")
        self.assertIn("converged at tick 4", out)
        self.assertIn("launch roles restored at tick 8", out)
        self.assertIn("2.0s", out)
        self.assertEqual(err, "")


if __name__ == "__main__":
    unittest.main()