"""The declared-freshness stage's unit coverage — ci/stale_freshness.py
is the reference plant's consumer-boundary mirror of the rig's
declared-freshness-budget exercise (decision 45's `stale_after_ticks`,
WW-OPS-003's stale-data surface and WW-ALM-003's rule that stale data
never presents as a healthy last-known value), run on the deployment
the manifest declares against *this* composition's own budget
declaration. These tests pin, without launching the pair: the
consumer composition's declared contract as the stage reads it off the
emitted model, the served-surface projections the walk judges, the
freeze/thaw seams, and the inconclusive / doctored-case
classifications `main` renders — plus the composition side of the
declaration itself: the emitted model carries the budget on the point
the model's `failover-select` primary reads, the manifest and the rig
definition record the re-emitted model's fingerprint, and a fresh
emission is byte-stable against the checked-in artifact."""
import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
_CI_DIR = _ROOT / "reference-plant" / "ci"
_STAGE_PATH = _CI_DIR / "stale_freshness.py"
_SCENARIO_PATH = _CI_DIR / "scenario.json"
_MODEL_PATH = _ROOT / "reference-plant" / "model" / "plant.json"
_MANIFEST_PATH = _ROOT / "reference-plant" / "deploy" / "manifest.json"
_COMPOSE_PATH = _ROOT / "reference-plant" / "deploy" / "compose.yaml"
_EMITTER = _ROOT / "target" / "debug" / "pump-station"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


stage = load(_STAGE_PATH, "stale_freshness")
model = json.loads(_MODEL_PATH.read_text())


def argv(tamper=None):
    args = [
        "stale_freshness.py",
        "--plant-server", "/nonexistent/dcs-plant-server",
        "--controller", "/nonexistent/dcs-controller",
        "--model", str(_MODEL_PATH),
        "--dynamics", "/nonexistent/dynamics.json",
        "--scenario", str(_SCENARIO_PATH),
        "--manifest", str(_MANIFEST_PATH),
    ]
    if tamper is not None:
        args += ["--tamper", tamper]
    return args


def run_main(tamper=None, outcome=None):
    """`main()` against a stubbed pass — `outcome` the return value
    or the exception stale_freshness_pass raises. Returns
    `(rc, stdout, stderr)`."""
    if isinstance(outcome, BaseException):
        stub = mock.Mock(side_effect=outcome)
    else:
        stub = mock.Mock(return_value=outcome)
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", argv(tamper)), \
            mock.patch.object(stage, "stale_freshness_pass", stub), \
            contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        rc = stage.main()
    return rc, out.getvalue(), err.getvalue()


class CompositionContract(unittest.TestCase):
    """The consumer composition's side of the declaration: the emitted
    model carries `stale_after_ticks` on the point its
    `failover-select` primary reads, the backup level beside it declares
    none, and the deployment records the re-emitted model's
    fingerprint. Without the declaration the stage has nothing to
    exercise — and says so, naming the missing contract."""

    def test_the_budgeted_point_is_the_selector_primary(self):
        points = stage.io_points(model)
        budgeted = [
            entry["id"] for entry in points.values()
            if entry.get("stale_after_ticks") is not None
        ]
        self.assertEqual(budgeted, [10])
        self.assertEqual(points[10]["direction"], "in")
        self.assertTrue(points[10]["channel"])

    def test_the_budget_sits_under_the_declared_failover_budget(self):
        # Comfortably under the manifest's declared switchover bound, so
        # staleness presents inside the writer-loss window rather than
        # after the pair has already switched.
        manifest = json.loads(_MANIFEST_PATH.read_text())
        budget = next(
            entry["failover_budget"]
            for entry in manifest["controllers"]
            if "failover_budget" in entry
        )
        declared = stage.io_points(model)[10]["stale_after_ticks"]
        self.assertLess(declared, budget)

    def test_the_unbudgeted_neighbour_shares_the_field_step(self):
        budgeted = stage.io_points(model)[10]
        neighbour = stage.io_points(model)[11]
        self.assertEqual(neighbour["channel"]["device"],
                         budgeted["channel"]["device"])
        self.assertNotEqual(neighbour["id"], budgeted["id"])
        self.assertIsNone(neighbour.get("stale_after_ticks"))

    def test_the_stage_reads_the_declared_contract_off_the_model(self):
        surface = stage.contract_surface(model)
        self.assertIsNotNone(surface)
        declaration, budget, neighbour, annunciated = surface
        self.assertEqual(declaration["id"], 10)
        self.assertEqual(budget, 2)
        self.assertEqual(neighbour["id"], 11)
        # The annunciation the stale presentation must drive: the
        # selector's own `backup_active` output.
        self.assertEqual(annunciated, 216)

    def test_a_composition_without_the_declaration_is_inconclusive(self):
        # A model whose budgeted point is not the selector's primary
        # carries no contract the stage can judge.
        stripped = dict(model)
        stripped["connections"] = [
            connection
            for connection in model["connections"]
            if (connection.get("to") or {}).get("port", {}).get("name")
            != "primary"
        ]
        self.assertIsNone(stage.contract_surface(stripped))

    def test_the_records_carry_the_emitted_fingerprint(self):
        if not _EMITTER.is_file():
            self.skipTest("the composition's emitter is not built here")
        emitted = subprocess.run(
            [_EMITTER], capture_output=True, text=True, check=True
        ).stdout
        self.assertEqual(
            emitted, _MODEL_PATH.read_text(),
            "a fresh emission differs from the checked-in model",
        )
        recorded = json.loads(_MANIFEST_PATH.read_text())["model"][
            "fingerprint"
        ]
        fingerprint = subprocess.run(
            [_EMITTER, "--fingerprint"], capture_output=True, text=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(recorded, fingerprint)
        self.assertIn(recorded, _COMPOSE_PATH.read_text())


class Projections(unittest.TestCase):
    """The served-surface projections the walk judges."""

    def test_quality_words_normalize_the_wire_shapes(self):
        self.assertEqual(stage.quality_key("good"), "good")
        self.assertEqual(
            stage.quality_key({"uncertain": "stale"}), "uncertain:stale"
        )
        self.assertEqual(
            stage.quality_key({"bad": "communication_fault"}),
            "bad:communication_fault",
        )
        self.assertEqual(stage.quality_key(None), "missing")

    def test_a_row_projects_the_three_served_surfaces(self):
        snapshot = {
            "points": [
                {"point": 10, "sample": {
                    "value": {"float": 4.5},
                    "quality": {"uncertain": "stale"}, "tick": 8}},
                {"point": 11, "sample": {
                    "value": {"float": 4.45}, "quality": "good",
                    "tick": 8}},
                {"point": 216, "sample": {
                    "value": {"bool": True}, "quality": "good", "tick": 8}},
            ]
        }
        self.assertEqual(
            stage.row(snapshot, 10, 11, 216),
            {"budgeted": "uncertain:stale", "unbudgeted": "good",
             "backup_active": True},
        )

    def test_an_absent_point_reads_no_sample(self):
        self.assertIsNone(stage.served_sample({"points": []}, 10))

    def test_a_retained_history_projects_seq_and_word(self):
        payload = [{
            "point": 10,
            "samples": [
                {"seq": 1, "sample": {"quality": "good"}},
                {"seq": 2, "sample": {"quality": {"uncertain": "stale"}}},
            ],
        }]
        with mock.patch.object(
            stage, "http", return_value=payload
        ):
            self.assertEqual(
                stage.stale_history("http://peer", 10),
                [[1, "good"], [2, "uncertain:stale"]],
            )

    def test_a_history_answer_for_another_point_reads_empty(self):
        with mock.patch.object(stage, "http", return_value=[]):
            self.assertEqual(stage.stale_history("http://peer", 10), [])


class Seams(unittest.TestCase):
    """The writer-freeze seam the writer-loss induction uses."""

    def test_a_frozen_writer_is_stopped_and_thawed(self):
        process = mock.Mock()
        process.poll.return_value = None
        stage.freeze(process)
        self.assertEqual(
            process.send_signal.call_args[0][0].name, "SIGSTOP")
        stage.thaw(process)
        self.assertEqual(
            process.send_signal.call_args[0][0].name, "SIGCONT")

    def test_a_dead_writer_refuses_the_freeze(self):
        process = mock.Mock()
        process.poll.return_value = 0
        with self.assertRaises(stage.Abort):
            stage.freeze(process)

    def test_a_dead_writer_is_left_alone_by_the_thaw(self):
        process = mock.Mock()
        process.poll.return_value = 1
        stage.thaw(process)
        process.send_signal.assert_not_called()

    def test_the_declared_margin_bounds_the_measured_lag(self):
        self.assertEqual(stage.ARRIVAL_SLACK, 4)
        self.assertGreater(stage.ARRIVAL_SLACK, 0)


class Inconclusive(unittest.TestCase):
    """The composition-precedence classification: a composition that
    carries no such contract renders a stable digest line and a zero
    exit, and a doctored case over an inconclusive run still fails."""

    def test_main_reports_the_inconclusive_digest(self):
        rc, out, err = run_main(
            outcome=stage.Inconclusive(
                "the emitted composition declares no freshness budget",
                "the composition carries 42 io points",
            )
        )
        self.assertEqual(rc, 0)
        self.assertIn(
            "stale-freshness-digest inconclusive — the emitted "
            "composition declares no freshness budget",
            out,
        )
        self.assertIn("42 io points", err)

    def test_two_inconclusive_passes_render_identically(self):
        first = run_main(outcome=stage.Inconclusive("pre-contract"))
        second = run_main(outcome=stage.Inconclusive("pre-contract"))
        self.assertEqual(first[0], 0)
        self.assertEqual(first[1], second[1])

    def test_a_doctored_case_over_an_inconclusive_run_fails(self):
        rc, _out, err = run_main(
            tamper="expect-last-good",
            outcome=stage.Inconclusive("pre-contract"),
        )
        self.assertEqual(rc, 1)
        self.assertIn(stage.TAMPER_EVIDENCE, err)


class Classification(unittest.TestCase):
    """main()'s exit classification over the pass result — failures
    stream to stderr prefixed by the stage's stem, a doctored case may
    never exit zero, and a clean pass renders the sha digest line."""

    def test_failures_report_by_name(self):
        rc, out, err = run_main(
            outcome=([], {}, ["the unbudgeted neighbour presented "
                              "bad:communication_fault"])
        )
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn(
            "stale-freshness: the unbudgeted neighbour presented "
            "bad:communication_fault",
            err,
        )

    def test_an_abort_reports_by_name(self):
        rc, _out, err = run_main(
            outcome=stage.Abort("the manifest declares no standby pair")
        )
        self.assertEqual(rc, 1)
        self.assertIn(
            "stale-freshness: the manifest declares no standby pair", err)

    def test_a_doctored_case_never_passes_silently(self):
        rc, _out, err = run_main(
            tamper="expect-last-good", outcome=([], {}, [])
        )
        self.assertEqual(rc, 1)
        self.assertIn("passed silently", err)

    def test_a_clean_pass_renders_the_digest_line(self):
        evidence = {
            "surface": {"budget": 2, "budgeted": 10, "unbudgeted": 11},
            "lag": 3,
            "history": [[1, "good"], [2, "uncertain:stale"]],
        }
        rc, out, _err = run_main(
            outcome=([{"phase": "converge"}], evidence, [])
        )
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"^stale-freshness-digest [0-9a-f]{64} — ")
        self.assertIn("the declared budget of 2 on point 10", out)
        self.assertIn("presented stale 3 served reads", out)
        self.assertIn("the pair's launch roles stand", out)


if __name__ == "__main__":
    unittest.main()