"""The settled_receipt_arbitration leg's unit coverage — ci/legs/
settled_receipt_arbitration.py is the reference plant's
consumer-boundary mirror of the qa rig's settled-receipt-arbitration
scenario (`1970_settled_receipt_arbitration.py`), proving the #690
contract on the released pair: a raced admission settled to two
different terminal verdicts at one submission index across the
promotion boundary's carry, both peers' served receipts converging on
one arbitrated verdict inside the dual-standby adoption window, and
one `command_settled` per admission per peer's durable journal. These
tests pin, without launching the pair: the leg's registration record,
its receipt/journal projections, the arbitration window's audit, the
contract-surface gate that classifies a pre-contract release
inconclusive, and the inconclusive / doctored-case classifications the
harness relies on — the new leg's evidence lines and inconclusive
handling asserted as the issue requires.
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
_LEG_PATH = _CI_DIR / "legs" / "settled_receipt_arbitration.py"
_MODEL_PATH = _ROOT / "reference-plant" / "model" / "plant.json"
_SCENARIO_PATH = _CI_DIR / "scenario.json"
_MANIFEST_PATH = _ROOT / "reference-plant" / "deploy" / "manifest.json"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


legs = load(_CI_DIR / "legs.py", "legs")
leg = load(_LEG_PATH, "settled_receipt_arbitration")

POINT = 1080
COMMAND = {
    "write_value": {"kind": "bool", "point": POINT, "value": {"bool": True}}
}
ACTOR = leg.ACTOR


def argv(tamper=None):
    args = [
        "settled_receipt_arbitration.py",
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
    or the exception arbitration_pass raises. Returns
    `(rc, stdout, stderr)`."""
    if isinstance(outcome, BaseException):
        stub = mock.Mock(side_effect=outcome)
    else:
        stub = mock.Mock(return_value=outcome)
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", argv(tamper)), \
            mock.patch.object(leg, "arbitration_pass", stub), \
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


def settled_entry(seq, tick, command=COMMAND, actor=ACTOR,
                  outcome=None):
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
        record = discovered["settled_receipt_arbitration.py"]
        self.assertEqual(record["stem"], "settled-receipt-arbitration")
        self.assertEqual(record["order"], 595)

    def test_the_named_diagnostics_follow_the_stem_convention(self):
        # The issue names settled-arbitration-failed and
        # settled-arbitration-nondeterministic — the driver's defaults
        # off the file stem, the failed one declared in the literal
        # because it does not spell the stem.
        self.assertEqual(leg.LEG["failed"],
                         "settled-arbitration-failed")
        self.assertEqual(leg.LEG["passes"], "settled-receipt-arbitration")

    def test_the_doctored_case_carries_named_evidence(self):
        tampers = {entry["name"]: entry for entry in leg.LEG["tampers"]}
        self.assertEqual(set(tampers), {"contradictory-pair"})
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


class Projections(unittest.TestCase):
    """The receipt and journal projections the audit reads — the
    admission's command identity, the verdict the convergence audit
    compares, and the `command_settled` records projected to their
    normalized settle rows."""

    def test_verdict_carries_the_apply_tick(self):
        self.assertEqual(
            leg.verdict(receipt(outcome={"applied": {"tick": 12}})),
            "applied@12")
        self.assertEqual(
            leg.verdict(receipt(outcome={"rejected": {"reason": {
                "superseded": {"point": POINT}}}})),
            "superseded")
        # The contradiction the fix arbitrates is two `applied`
        # verdicts at two ticks: without the tick a serving log
        # answering only `applied` could not tell convergence from
        # flapping.
        self.assertNotEqual(
            leg.verdict(receipt(outcome={"applied": {"tick": 10}})),
            leg.verdict(receipt(outcome={"applied": {"tick": 11}})))

    def test_admission_receipts_filters_by_command(self):
        receipts = [
            receipt(),
            receipt(command={"write_value": {"point": 1}}),
        ]
        self.assertEqual(leg.admission_receipts(receipts, COMMAND),
                         [receipts[0]])

    def test_settle_rows_projects_in_seq_order(self):
        # Every `command_settled` record in the entries, normalized —
        # the admission selection is `admission_settles`' half.
        entries = [
            settled_entry(3, 7, outcome={"applied": {"tick": 9}}),
            entry(4, 8, {"point_changed": {"point": POINT, "to": {}}}),
            settled_entry(5, 9, outcome={"applied": {"tick": 10}}),
        ]
        self.assertEqual(
            leg.settle_rows(entries),
            [{"seq": 3, "tick": 7, "actor": ACTOR,
              "outcome": "applied@9"},
             {"seq": 5, "tick": 9, "actor": ACTOR,
              "outcome": "applied@10"}],
        )

    def test_settle_rows_ignores_other_events(self):
        entries = [
            entry(3, 7, {"role_changed": {"from": "active"}}),
            entry(4, 8, {"command_admitted": {"receipt": receipt()}}),
        ]
        self.assertEqual(leg.settle_rows(entries), [])

    def test_admission_settles_selects_the_admission_only(self):
        # The admission's identity on this leg is its command: every
        # settle answering it counts toward the once-per-admission
        # bound, whichever peer recorded it and however many times.
        entries = [
            settled_entry(3, 7, outcome={"applied": {"tick": 9}}),
            settled_entry(4, 8, command={"write_value": {
                "kind": "bool", "point": 1, "value": {"bool": True}}},
                outcome={"applied": {"tick": 9}}),
            settled_entry(5, 9, outcome={"applied": {"tick": 10}}),
        ]
        self.assertEqual(
            [row["seq"] for row in
             leg.settle_rows(leg.admission_settles(entries, COMMAND))],
            [3, 5],
        )

    def test_journal_entries_reads_the_durable_record(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "journal.jsonl"
            path.write_text(
                json.dumps({"run_boundary": {"run": 1, "tick": 0}})
                + "\n"
                + json.dumps({"entry": settled_entry(1, 2, outcome={
                    "applied": {"tick": 2}})})
                + "\n"
            )
            entries = leg.journal_entries(str(path))
            self.assertEqual(len(entries), 1)
            self.assertEqual(
                leg.settle_rows(entries),
                [{"seq": 1, "tick": 2, "actor": ACTOR,
                  "outcome": "applied@2"}],
            )

    def test_following_reads_both_resolved_sync_states(self):
        for sync in ({"tracking": {"aligned": 9}}, {"orphaned": {}}):
            report = {"role": "standby", "sync": sync}
            with mock.patch.object(
                leg.pair, "get", return_value=report
            ):
                self.assertEqual(
                    leg.following("http://peer", []), report)
        for report in (
            {"role": "standby", "sync": {"unsynchronized": {}}},
            {"role": "active", "sync": {"tracking": {}}},
            {"role": "standby"},
        ):
            with mock.patch.object(
                leg.pair, "get", return_value=report
            ):
                self.assertIsNone(leg.following("http://peer", []))


class ArbitrationWindow(unittest.TestCase):
    """The adoption window's audit — the contract's own decision: the
    pair's served receipts converged on one arbitrated verdict that
    never moved, or the named fault the run reports. The doctored
    negative the leg's own tamper plants is the first case here: a
    contradictory pair asserted converged."""

    def test_a_converged_window_carries_no_fault(self):
        settled = {"duty": "applied@10", "standby": "applied@10"}
        self.assertIsNone(leg.window_fault([settled] * 4))

    def test_the_oscillating_window_is_nondeterministic(self):
        # The finding's evidence: both /receipts flapping at scan
        # cadence while the journal kept settling.
        fault = leg.window_fault([
            {"duty": "applied@10", "standby": "applied@10"},
            {"duty": "applied@11", "standby": "applied@11"},
        ])
        self.assertEqual(fault[0],
                         "settled-arbitration-nondeterministic")
        self.assertIn("moved across the adoption window", fault[1])

    def test_the_contradictory_pair_is_nondeterministic(self):
        # The leg's doctored case: the pair asserted converged while
        # each peer still serves its own settlement.
        fault = leg.window_fault([
            {"duty": "applied@10", "standby": "applied@11"},
        ])
        self.assertEqual(fault[0],
                         "settled-arbitration-nondeterministic")
        self.assertIn("a contradictory pair", fault[1])

    def test_a_window_without_a_terminal_verdict_is_failed(self):
        fault = leg.window_fault([{"duty": "applied@10",
                                   "standby": None}])
        self.assertEqual(fault[0], "settled-arbitration-failed")
        self.assertIn("no terminal verdict", fault[1])

    def test_an_empty_window_is_failed(self):
        fault = leg.window_fault([])
        self.assertEqual(fault[0], "settled-arbitration-failed")
        self.assertIn("no observation", fault[1])


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
                "http://peer", "GET /checkpoint on duty", [])

    def test_the_full_surface_passes(self):
        checkpoint = {
            "receipts": [receipt(outcome={"applied": {"tick": 9}})],
            "command_admission": {"attempts": 3},
        }
        self.assertEqual(self.surface(checkpoint), checkpoint)

    def test_each_missing_field_is_inconclusive(self):
        for checkpoint in (
            {"command_admission": {"attempts": 3}},
            {"receipts": []},
            {"receipts": [], "command_admission": {}},
            {"receipts": [], "command_admission": {"attempts": "3"}},
        ):
            with self.assertRaises(leg.Inconclusive, msg=checkpoint):
                self.surface(checkpoint)


class Inconclusive(unittest.TestCase):
    """The release-precedence classification: a pinned release
    predating the settled-receipt contract raises the leg's
    Inconclusive, which main renders as a stable digest line and a
    zero exit — and a doctored case over an inconclusive run still
    fails, since it can name no evidence."""

    def test_main_reports_the_inconclusive_digest(self):
        rc, out, err = run_main(
            outcome=leg.Inconclusive(
                "the pinned release predates the settled-receipt "
                "contract"
            )
        )
        self.assertEqual(rc, 0)
        self.assertIn(
            "settled-arbitration-digest inconclusive — the pinned "
            "release predates",
            out,
        )
        self.assertIn("settled-arbitration: inconclusive", err)

    def test_two_inconclusive_passes_render_identically(self):
        first = run_main(outcome=leg.Inconclusive("pre-contract"))
        second = run_main(outcome=leg.Inconclusive("pre-contract"))
        self.assertEqual(first[0], 0)
        self.assertEqual(first[1], second[1])

    def test_a_doctored_case_over_an_inconclusive_run_fails(self):
        rc, out, err = run_main(
            tamper="contradictory-pair",
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
            outcome=([], {}, ["the served receipts never converged"])
        )
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn(
            "settled-arbitration: the served receipts never converged",
            err,
        )

    def test_an_abort_reports_by_name(self):
        rc, out, err = run_main(
            outcome=leg.Abort("the pair never converged")
        )
        self.assertEqual(rc, 1)
        self.assertIn("settled-arbitration: the pair never converged", err)

    def test_a_doctored_case_never_passes_silently(self):
        rc, out, err = run_main(
            tamper="contradictory-pair", outcome=([], {}, [])
        )
        self.assertEqual(rc, 1)
        self.assertIn("passed silently", err)

    def test_a_doctored_case_carrying_failures_still_fails(self):
        rc, _, err = run_main(
            tamper="contradictory-pair",
            outcome=([], {}, ["a contradictory pair reads the pair's "
                              "own settlements"]),
        )
        self.assertEqual(rc, 1)
        self.assertIn("a contradictory pair", err)

    def test_a_clean_pass_renders_the_digest_line(self):
        rc, out, err = run_main(
            outcome=([{"phase": "converge"},
                      {"phase": "staged", "settled": {
                          "duty": ["applied@10"],
                          "standby": ["applied@11"]}},
                      {"phase": "window"}], {}, [])
        )
        self.assertEqual(rc, 0, err)
        self.assertIn("settled-arbitration-digest ", out)
        self.assertIn("one command_settled per admission per peer "
                      "journal", out)
        self.assertEqual(err, "")


if __name__ == "__main__":
    unittest.main()
