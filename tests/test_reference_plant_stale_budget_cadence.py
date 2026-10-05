"""The stale_budget_cadence leg's unit coverage — ci/legs/
stale_budget_cadence.py is the reference plant's consumer-boundary
mirror of the rig's cadence-domain exercise (the #1411 contract): a
tracking reader scanning faster than the field owner steps its inputs
must hold the declared freshness contract — the budgeted point stays
`Good` per the arrival period it has itself demonstrated, with no
journaled `quality_changed` traffic after that evidence — while a
reader paced like the owner keeps the declared staleness behavior at
the declared lag. These tests pin, without launching the pair: the
leg's registration record, the composition surface it reads, the
same-domain arrival-period projection the judgement is built on, the
durable `quality_changed` read, the asymmetry staging bound, and the
inconclusive / doctored-case classifications `main` renders."""
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
_CI_DIR = _ROOT / "reference-plant" / "ci"
_LEG_PATH = _CI_DIR / "legs" / "stale_budget_cadence.py"
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
leg = load(_LEG_PATH, "stale_budget_cadence")
model = json.loads(_MODEL_PATH.read_text())


def argv(tamper=None):
    args = [
        "stale_budget_cadence.py",
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
    """`main()` against a stubbed pass — `outcome` the return value or
    the exception stale_budget_cadence_pass raises. Returns
    `(rc, stdout, stderr)`."""
    if isinstance(outcome, BaseException):
        stub = mock.Mock(side_effect=outcome)
    else:
        stub = mock.Mock(return_value=outcome)
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", argv(tamper)), \
            mock.patch.object(leg, "stale_budget_cadence_pass", stub), \
            contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        rc = leg.main()
    return rc, out.getvalue(), err.getvalue()


def row(tick, value, quality="good"):
    return {"tick": tick, "budgeted": quality, "unbudgeted": "good",
            "witness": value}


class Registration(unittest.TestCase):
    """The leg's `LEG` literal — a unique declared order in the pair
    stage, the stem-derived `stale-budget-cadence-*` diagnostic family,
    and the doctored case naming the evidence the honest run reports."""

    def test_the_leg_registers_in_the_pair_stage(self):
        discovered = {
            Path(record["file"]).name: record
            for record in legs.discover(str(_CI_DIR / "legs"))
        }
        record = discovered["stale_budget_cadence.py"]
        self.assertEqual(record["stem"], "stale-budget-cadence")
        self.assertEqual(record["order"], 891)
        orders = [
            item["order"]
            for item in legs.discover(str(_CI_DIR / "legs"))
        ]
        self.assertEqual(len(orders), len(set(orders)))

    def test_the_diagnostics_follow_the_file_stem(self):
        self.assertNotIn("failed", leg.LEG)
        self.assertEqual(leg.LEG["passes"], "stale-budget-cadence-leg")
        self.assertEqual(
            legs.leg_stem("stale_budget_cadence.py"), "stale-budget-cadence"
        )

    def test_the_doctored_case_carries_named_evidence(self):
        tampers = {entry["name"]: entry for entry in leg.LEG["tampers"]}
        self.assertEqual(set(tampers), {"expect-patience"})
        source = _LEG_PATH.read_text()
        for name, record in tampers.items():
            self.assertTrue(record["evidence"], name)
            for evidence in record["evidence"]:
                self.assertIn(evidence, source, name)
            for field in ("passed", "missed"):
                self.assertIsInstance(record[field], str)


class Composition(unittest.TestCase):
    """The composition surface the leg reads: one budgeted field input
    and one unbudgeted neighbour sharing a field step — the contrast the
    cadence judgement is legible against."""

    def test_the_surface_names_the_budget_and_the_witness(self):
        contract = leg.surface(model)
        self.assertIsNotNone(contract)
        budgeted, budget, witness = contract
        self.assertEqual(budgeted["id"], 10)
        self.assertEqual(budget, 2)
        self.assertEqual(witness["id"], 11)
        self.assertEqual(witness["channel"]["device"],
                         budgeted["channel"]["device"])

    def test_a_composition_without_the_pair_is_declined(self):
        stripped = dict(model)
        stripped["io_points"] = [
            entry for entry in model["io_points"]
            if entry.get("stale_after_ticks") is None
        ]
        self.assertIsNone(leg.surface(stripped))

    def test_two_budgets_are_declined(self):
        # The judgement needs one budget and one contrast; two budgets
        # leave no unbudgeted neighbour to read against.
        doubled = dict(model)
        doubled["io_points"] = [
            dict(entry)
            if entry["id"] != 11
            else {**entry, "stale_after_ticks": 3}
            for entry in model["io_points"]
        ]
        self.assertIsNone(leg.surface(doubled))


class Projections(unittest.TestCase):
    """The served-surface projections the judgement is built on: the
    quality word, one reader-tick observation, the same-domain arrival
    gaps, and the durable `quality_changed` read."""

    def test_arrival_gaps_measure_the_field_period_in_reader_ticks(self):
        # The witness changed at reader ticks 4, 8, 12 — the field steps
        # once per owner scan and the reader runs four scans to it, so
        # the demonstrated period is 4 reader ticks.
        rows = [
            row(1, "4.5"), row(2, "4.5"), row(5, "4.7"), row(6, "4.7"),
            row(9, "4.9"), row(13, "5.1"),
        ]
        self.assertEqual(leg.gaps(rows), [4, 4])

    def test_an_unmoving_witness_demonstrates_nothing(self):
        rows = [row(1, "4.5"), row(2, "4.5"), row(3, "4.5")]
        self.assertEqual(leg.gaps(rows), [])

    def test_a_row_projects_the_served_surfaces(self):
        snapshot = {"points": [
            {"point": 10, "sample": {"quality": {"uncertain": "stale"},
                                     "value": {"float": 4.5}}},
            {"point": 11, "sample": {"quality": "good",
                                     "value": {"float": 4.45}}},
        ]}
        answers = [
            {"role": "standby", "sync": {"orphaned": {}}, "tick": 17},
            snapshot,
        ]
        with mock.patch.object(leg, "http", side_effect=answers):
            self.assertEqual(
                leg.observation("http://reader", 10, 11),
                {"tick": 17, "budgeted": "uncertain:stale",
                 "unbudgeted": "good", "witness": '{"float": 4.45}'},
            )

    def test_the_durable_quality_trail_projects_seq_tick_and_word(self):
        records = [
            {"entry": {"seq": 1, "tick": 3, "event": {"quality_changed": {
                "point": 10, "from": "good",
                "to": {"uncertain": "stale"}}}}},
            {"entry": {"seq": 2, "tick": 9, "event": {"quality_changed": {
                "point": 11, "from": "good", "to": "bad"}}}},
        ]
        with tempfile.NamedTemporaryFile(
            "w", suffix=".jsonl", delete=False
        ) as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
            path = handle.name
        self.assertEqual(
            leg.quality_changes(path, 10), [(1, 3, "uncertain:stale")]
        )

    def test_an_absent_journal_reads_no_records(self):
        self.assertEqual(leg.quality_changes("/nonexistent", 10), [])

    def test_the_cold_start_floor_isolates_a_post_evidence_record(self):
        # The reader's cold start may leave the contract's allowed pair
        # behind; a record landing after the arrival evidence is the
        # defect's journaled flap and must be legible past that floor.
        records = [
            {"entry": {"seq": 1, "tick": 6, "event": {"quality_changed": {
                "point": 10, "from": None, "to": "good"}}}},
            {"entry": {"seq": 304, "tick": 8, "event": {
                "quality_changed": {"point": 10, "from": "good",
                                    "to": {"uncertain": "stale"}}}}},
            {"entry": {"seq": 306, "tick": 10, "event": {
                "quality_changed": {"point": 10,
                                    "from": {"uncertain": "stale"},
                                    "to": "good"}}}},
            {"entry": {"seq": 400, "tick": 40, "event": {
                "quality_changed": {"point": 10, "from": "good",
                                    "to": {"uncertain": "stale"}}}}},
        ]
        with tempfile.NamedTemporaryFile(
            "w", suffix=".jsonl", delete=False
        ) as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
            path = handle.name
        changes = leg.quality_changes(path, 10)
        self.assertEqual(len(changes), 4)
        self.assertEqual(
            changes[3:], [(400, 40, "uncertain:stale")]
        )


class Staging(unittest.TestCase):
    """The cadence staging: the per-member `--dt` lever, the measured
    asymmetry bound that keeps the leg from passing vacuously, and the
    writer-freeze seam the control arm uses."""

    def test_the_cadence_lever_is_several_scans_per_owner_step(self):
        self.assertGreaterEqual(leg.CADENCE_RATIO, 3)
        self.assertLess(leg.CADENCE_RATIO, 10)
        # The measured bound sits below the declared ratio so a reader
        # paced like the owner can never clear it.
        self.assertLess(leg.MIN_MEASURED_RATIO, leg.CADENCE_RATIO)

    def test_the_arrival_warm_window_completes_two_gaps(self):
        self.assertEqual(leg.ARRIVAL_WARM, 2)

    def test_a_frozen_writer_is_stopped_and_thawed(self):
        process = mock.Mock()
        process.poll.return_value = None
        leg.freeze(process)
        self.assertEqual(
            process.send_signal.call_args[0][0].name, "SIGSTOP")
        leg.thaw(process)
        self.assertEqual(
            process.send_signal.call_args[0][0].name, "SIGCONT")

    def test_a_dead_writer_refuses_the_freeze(self):
        process = mock.Mock()
        process.poll.return_value = 0
        with self.assertRaises(leg.Abort):
            leg.freeze(process)


class Inconclusive(unittest.TestCase):
    """The release-precedence classification: a pinned release
    predating the cadence contract, or a harness admitting no
    asymmetric pacing, renders a stable digest line and a zero exit —
    and a doctored case over an inconclusive run still fails."""

    def test_main_reports_the_inconclusive_digest(self):
        rc, out, err = run_main(
            outcome=leg.Inconclusive(
                "the harness admitted no asymmetric pacing",
                "the owner gained 6 run ticks while the subject gained 6",
            )
        )
        self.assertEqual(rc, 0)
        self.assertIn(
            "stale-budget-cadence-digest inconclusive — the harness "
            "admitted no asymmetric pacing",
            out,
        )
        self.assertIn("the subject gained 6", err)

    def test_two_inconclusive_passes_render_identically(self):
        first = run_main(outcome=leg.Inconclusive("pre-contract"))
        second = run_main(outcome=leg.Inconclusive("pre-contract"))
        self.assertEqual(first[0], 0)
        self.assertEqual(first[1], second[1])

    def test_a_doctored_case_over_an_inconclusive_run_fails(self):
        rc, _out, err = run_main(
            tamper="expect-patience",
            outcome=leg.Inconclusive("pre-contract"),
        )
        self.assertEqual(rc, 1)
        self.assertIn(leg.TAMPER_EVIDENCE, err)


class Classification(unittest.TestCase):
    """main()'s exit classification over the pass result — failures
    stream to stderr prefixed by the stem, a doctored case may never
    exit zero, and a clean pass renders the sha digest line."""

    def test_failures_report_by_name(self):
        rc, out, err = run_main(
            outcome=([], {}, ["the subject reader presented "
                              "uncertain:stale at reader tick 22"])
        )
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn(
            "stale-budget-cadence: the subject reader presented "
            "uncertain:stale at reader tick 22",
            err,
        )

    def test_an_abort_reports_by_name(self):
        rc, _out, err = run_main(
            outcome=leg.Abort("a reader never converged tracking")
        )
        self.assertEqual(rc, 1)
        self.assertIn(
            "stale-budget-cadence: a reader never converged tracking", err)

    def test_a_doctored_case_never_passes_silently(self):
        rc, _out, err = run_main(
            tamper="expect-patience", outcome=([], {}, [])
        )
        self.assertEqual(rc, 1)
        self.assertIn("passed silently", err)

    def test_a_clean_pass_renders_the_digest_line(self):
        evidence = {
            "surface": {"budget": 2, "budgeted": 10, "witness": 11},
            "asymmetry": {"measured": 4.0, "owner_gain": 6,
                          "subject_gain": 24},
            "demonstrated": [4, 4],
            "control_lag": 4,
        }
        rc, out, _err = run_main(
            outcome=([{"phase": "converge"}], evidence, [])
        )
        self.assertEqual(rc, 0)
        self.assertRegex(
            out, r"^stale-budget-cadence-digest [0-9a-f]{64} — "
        )
        self.assertIn("ran 4.0x the field owner's cadence", out)
        self.assertIn("held at 4 rows", out)
        self.assertIn("the pair's launch roles stand", out)


if __name__ == "__main__":
    unittest.main()
