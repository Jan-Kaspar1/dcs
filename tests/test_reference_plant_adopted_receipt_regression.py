"""The adopted_receipt_regression leg's unit coverage —
reference-plant/ci/legs/adopted_receipt_regression.py is the
reference plant's consumer-boundary mirror of the qa rig's
adopted-receipt-regression scenario
(`qa_lane/scenarios/1980_adopted_receipt_regression.py`), proving the
#709 contract on the released pair: the standby's pull carries a
pending admission while the owner's own boundary has not run, the
owner's later adoption of that staler window keeps the terminal
verdict, and each peer carries exactly one `command_settled` for the
admission. These tests pin, without launching the pair: the leg's
registration record, its receipt/journal projections, the adoption
window's audit, the contract-surface gate that classifies a
pre-contract release inconclusive, and the inconclusive /
doctored-case classifications the harness relies on — the new leg's
evidence lines and inconclusive handling asserted as the issue
requires.
"""
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
_LEG_PATH = _CI_DIR / "legs" / "adopted_receipt_regression.py"
_MODEL_PATH = _ROOT / "reference-plant" / "model" / "plant.json"
_SCENARIO_PATH = _CI_DIR / "scenario.json"
_MANIFEST_PATH = _ROOT / "reference-plant" / "deploy" / "manifest.json"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


legs = load(_CI_DIR / "legs.py", "legs")
leg = load(_LEG_PATH, "adopted_receipt_regression")

POINT = 1080
COMMAND = {
    "write_value": {"kind": "bool", "point": POINT, "value": {"bool": True}}
}
ACTOR = leg.ACTOR


def argv(tamper=None):
    args = [
        "adopted_receipt_regression.py",
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
    or the exception regression_pass raises. Returns
    `(rc, stdout, stderr)`."""
    if isinstance(outcome, BaseException):
        stub = mock.Mock(side_effect=outcome)
    else:
        stub = mock.Mock(return_value=outcome)
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", argv(tamper)), \
            mock.patch.object(leg, "regression_pass", stub), \
            contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        rc = leg.main()
    return rc, out.getvalue(), err.getvalue()


def receipt(command=COMMAND, actor=ACTOR, outcome=None):
    return {
        "command": command,
        "actor": actor,
        "outcome": outcome if outcome is not None else {"accepted": {}},
    }


def entry(seq, tick, event):
    return {"seq": seq, "tick": tick, "event": event}


def settled_entry(seq, tick, command=COMMAND, actor=ACTOR, outcome=None):
    return entry(
        seq,
        tick,
        {"command_settled": {"receipt": receipt(
            command, actor, outcome)}},
    )


class Registration(unittest.TestCase):
    """The leg's `LEG` literal — the pair stage's discovery contract:
    the declared order is unique across the directory, the stem and
    the named diagnostics follow the file-name convention, and the
    doctored case declares the evidence the honest run reports."""

    def test_the_leg_registers_in_the_pair_stage(self):
        discovered = {
            Path(record["file"]).name: record
            for record in legs.discover(str(_CI_DIR / "legs"))
        }
        record = discovered["adopted_receipt_regression.py"]
        self.assertEqual(record["stem"], "adopted-receipt-regression")
        self.assertEqual(record["order"], 605)

    def test_the_named_diagnostics_follow_the_stem_convention(self):
        # The issue names receipt-regression-failed and
        # receipt-regression-nondeterministic; the driver derives its
        # failed diagnostic off the file stem unless the literal
        # declares one, so the leg spells the issue's name — the
        # settled-receipt-arbitration leg's convention — while
        # receipt-regression-nondeterministic is what the leg's own
        # evidence lines report.
        self.assertEqual(leg.LEG["passes"], "adopted-receipt-regression")
        self.assertEqual(leg.LEG["failed"], "receipt-regression-failed")

    def test_the_doctored_case_carries_named_evidence(self):
        tampers = {entry["name"]: entry for entry in leg.LEG["tampers"]}
        self.assertEqual(set(tampers), {"expect-regressed"})
        # The declared evidence must be the diagnostic the leg actually
        # prints — pin it against the source's failure lines so a
        # drifted message cannot pass the harness's substring check by
        # accident.
        source = _LEG_PATH.read_text()
        for record in tampers.values():
            self.assertTrue(record["evidence"])
            for evidence in record["evidence"]:
                self.assertIn(evidence, source)
            for field in ("passed", "missed"):
                self.assertIsInstance(record[field], str)


class Projections(unittest.TestCase):
    """The receipt and journal projections the audit reads — the
    admission's submission identity, the verdict the regression audit
    compares, and the `command_settled` records normalized to their
    settle rows."""

    def test_verdict_carries_the_apply_tick(self):
        self.assertEqual(
            leg.verdict(receipt(outcome={"applied": {"tick": 61481}})),
            "applied@61481",
        )
        self.assertEqual(
            leg.verdict(receipt(outcome={"rejected": {"reason": {
                "superseded": {"point": POINT}}}})),
            "superseded",
        )
        self.assertEqual(leg.verdict(receipt()), "accepted")
        # The finding's duplicate was a replay of an already-recorded
        # verdict: without the tick a serving log answering only
        # `applied` could not tell the replay from a second settle.
        self.assertNotEqual(
            leg.verdict(receipt(outcome={"applied": {"tick": 61481}})),
            leg.verdict(receipt(outcome={"applied": {"tick": 61494}})),
        )

    def test_admission_receipts_filters_by_submission(self):
        receipts = [
            receipt(),
            receipt(command={"write_value": {"point": 1}}),
            receipt(actor="someone-else"),
        ]
        self.assertEqual(
            leg.admission_receipts(receipts, COMMAND, ACTOR),
            [receipts[0]],
        )

    def test_settle_rows_projects_in_seq_order(self):
        # Every `command_settled` record in the entries, normalized —
        # the admission selection is the same filter's half.
        entries = [
            settled_entry(284, 61481, outcome={"applied": {"tick": 61481}}),
            entry(285, 61482, {"point_changed": {"point": POINT}}),
            settled_entry(287, 61494, outcome={"applied": {"tick": 61481}}),
        ]
        self.assertEqual(
            leg.settle_rows(entries, COMMAND, ACTOR),
            [(284, 61481, "applied"), (287, 61494, "applied")],
        )

    def test_settle_rows_ignores_other_events(self):
        entries = [
            entry(3, 7, {"role_changed": {"from": "active"}}),
            entry(4, 8, {"command_admitted": {"receipt": receipt()}}),
        ]
        self.assertEqual(leg.settle_rows(entries, COMMAND, ACTOR), [])

    def test_journal_entries_reads_the_durable_record(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "journal.jsonl"
            path.write_text(
                json.dumps({"run_boundary": {"run": 1, "tick": 0}})
                + "\n"
                + json.dumps({"entry": settled_entry(
                    1, 2, outcome={"applied": {"tick": 2}})})
                + "\n"
            )
            entries = leg.journal_entries(str(path))
            self.assertEqual(len(entries), 1)
            self.assertEqual(
                leg.settle_rows(entries, COMMAND, ACTOR),
                [(1, 2, "applied")],
            )


class AdoptionWindow(unittest.TestCase):
    """The adoption window's audit — the contract's own decision: the
    peer holding the settle keeps its terminal verdict across the pull
    that adopted a staler pending view, or the named fault the run
    reports. The doctored negative the leg's own tamper plants is the
    first case here: any observation accepted as regressed."""

    def test_a_held_window_carries_no_fault(self):
        self.assertIsNone(leg.regression_fault(
            {"duty": ["applied@61481"] * 4,
             "standby": ["applied@61481"] * 4},
            None,
        ))

    def test_the_pending_view_after_the_settle_is_nondeterministic(self):
        # The finding's evidence: the settling peer's receipt served
        # `accepted` again after its own settle journaled.
        fault = leg.regression_fault(
            {"duty": ["applied@61481", "accepted@61482"],
             "standby": ["applied@61481", "applied@61481"]},
            None,
        )
        self.assertEqual(fault[0], "receipt-regression-nondeterministic")
        self.assertIn("moved back to the pending view", fault[1])

    def test_the_settled_verdict_moving_is_nondeterministic(self):
        fault = leg.regression_fault(
            {"duty": ["applied@61481", "applied@61494"],
             "standby": ["applied@61494"]},
            None,
        )
        self.assertEqual(fault[0], "receipt-regression-nondeterministic")
        self.assertIn("changed verdict", fault[1])

    def test_the_peers_disagreeing_is_nondeterministic(self):
        fault = leg.regression_fault(
            {"duty": ["applied@61481"], "standby": ["applied@61494"]},
            None,
        )
        self.assertEqual(fault[0], "receipt-regression-nondeterministic")
        self.assertIn("disagree", fault[1])

    def test_a_peer_that_never_served_a_verdict_is_failed(self):
        fault = leg.regression_fault(
            {"duty": ["accepted@61482"], "standby": [None]},
            None,
        )
        self.assertEqual(fault[0], "receipt-regression-failed")
        self.assertIn("no terminal verdict", fault[1])

    def test_an_empty_window_is_failed(self):
        fault = leg.regression_fault({}, None)
        self.assertEqual(fault[0], "receipt-regression-failed")
        self.assertIn("no observation", fault[1])

    def test_the_doctored_case_is_reported_on_the_honest_record(self):
        fault = leg.regression_fault(
            {"duty": ["applied@61481"], "standby": ["applied@61481"]},
            "expect-regressed",
        )
        self.assertEqual(fault[0], "receipt-regression-failed")
        self.assertIn("accepted a regressed receipt", fault[1])


class ContractSurface(unittest.TestCase):
    """The release-precedence gate: the served checkpoint must carry
    the receipt window and the admission counters the audit correlates
    absolute submission indices by — any absence is the pinned release
    predating the contract, never a violation."""

    def surface(self, checkpoint):
        with mock.patch.object(
            leg.pair, "get", return_value=checkpoint
        ):
            return leg.receipt_window(
                "http://peer", "GET /checkpoint on duty", []
            )

    def test_the_full_surface_passes(self):
        checkpoint = {
            "receipts": [receipt(outcome={"applied": {"tick": 9}})],
            "command_admission": {"attempts": 3},
        }
        self.assertEqual(self.surface(checkpoint)[1], checkpoint["receipts"])

    def test_each_missing_field_is_inconclusive(self):
        for checkpoint in (
            {"command_admission": {"attempts": 3}},
            {"receipts": []},
            {"receipts": [], "command_admission": {}},
            {"receipts": [], "command_admission": {"attempts": "3"}},
        ):
            with self.assertRaises(leg.Inconclusive, msg=checkpoint):
                self.surface(checkpoint)

    def test_the_window_places_its_first_entry_in_the_sequence(self):
        checkpoint = {
            "receipts": [receipt(), receipt()],
            "command_admission": {"attempts": 7},
        }
        _window, receipts, base = self.surface(checkpoint)
        self.assertEqual(len(receipts), 2)
        self.assertEqual(base, 5)


class Inconclusive(unittest.TestCase):
    """The release-precedence classification: a pinned release
    predating the adopted-receipt contract raises the leg's
    Inconclusive, which main renders as a stable digest line and a
    zero exit — and a doctored case over an inconclusive run still
    fails, since it can name no evidence."""

    def test_main_reports_the_inconclusive_digest(self):
        rc, out, err = run_main(
            outcome=leg.Inconclusive(
                "the pinned release predates the adopted-receipt "
                "contract"
            )
        )
        self.assertEqual(rc, 0)
        self.assertIn(
            "receipt-regression-digest inconclusive — the pinned "
            "release predates",
            out,
        )
        self.assertIn("receipt-regression: inconclusive", err)

    def test_two_inconclusive_passes_render_identically(self):
        first = run_main(outcome=leg.Inconclusive("pre-contract"))
        second = run_main(outcome=leg.Inconclusive("pre-contract"))
        self.assertEqual(first[0], 0)
        self.assertEqual(first[1], second[1])

    def test_a_doctored_case_over_an_inconclusive_run_fails(self):
        rc, out, err = run_main(
            tamper="expect-regressed",
            outcome=leg.Inconclusive("pre-contract"),
        )
        self.assertEqual(rc, 1)
        self.assertIn("wanted the run to surface its named diagnostic",
                      err)


class Classification(unittest.TestCase):
    """main()'s exit classification over the pass result — failures
    stream to stderr prefixed by the stem, a doctored case may never
    exit zero, and a clean pass renders the sha digest line."""

    def test_failures_report_by_name(self):
        rc, out, err = run_main(
            outcome=([], {}, ["the owner regressed the settle"])
        )
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn(
            "receipt-regression: the owner regressed the settle", err
        )

    def test_an_abort_reports_by_name(self):
        rc, out, err = run_main(
            outcome=leg.Abort("the pair never converged")
        )
        self.assertEqual(rc, 1)
        self.assertIn("receipt-regression: the pair never converged", err)

    def test_a_doctored_case_never_passes_silently(self):
        rc, out, err = run_main(
            tamper="expect-regressed", outcome=([], {}, [])
        )
        self.assertEqual(rc, 1)
        self.assertIn("passed silently", err)

    def test_a_doctored_case_carrying_failures_still_fails(self):
        rc, _, err = run_main(
            tamper="expect-regressed",
            outcome=([], {}, ["the doctored expectation accepted the "
                              "regressed receipt"]),
        )
        self.assertEqual(rc, 1)
        self.assertIn("accepted the regressed receipt", err)

    def test_a_clean_pass_renders_the_digest_line(self):
        rc, out, err = run_main(
            outcome=([{"phase": "converge"},
                      {"phase": "settled", "view": "applied@61481"},
                      {"phase": "window"}], {}, [])
        )
        self.assertEqual(rc, 0, err)
        self.assertIn("receipt-regression-digest ", out)
        self.assertIn("one command_settled per admission on both served "
                      "and durable journals", out)
        self.assertEqual(err, "")


if __name__ == "__main__":
    unittest.main()