"""The receipt_index_collision leg's unit coverage — ci/legs/
receipt_index_collision.py is the reference plant's
consumer-boundary mirror of the qa rig's receipt-index-collision
scenario (`qa_lane/scenarios/1985_receipt_index_collision.py`),
proving the #775 contract on the released pair: a raced admission
superseded at the promote/fence window and a second admission minting
the same absolute index both keep their retrievable terminal verdict on
the pair's served surface, with one `command_settled` per admission on
each peer's journal. These tests pin, without launching the pair: the
leg's registration record, its receipt/journal projections, the
collision audit, the contract-surface gate that classifies a
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
_LEG_PATH = _CI_DIR / "legs" / "receipt_index_collision.py"
_MODEL_PATH = _ROOT / "reference-plant" / "model" / "plant.json"
_SCENARIO_PATH = _CI_DIR / "scenario.json"
_MANIFEST_PATH = _ROOT / "reference-plant" / "deploy" / "manifest.json"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


legs = load(_CI_DIR / "legs.py", "legs")
leg = load(_LEG_PATH, "receipt_index_collision")

POINT = 1080
RACED = {
    "write_value": {"kind": "bool", "point": POINT, "value": {"bool": True}}
}
MINTED = {
    "write_value": {"kind": "bool", "point": POINT, "value": {"bool": False}}
}
RACED_ACTOR = leg.RACED_ACTOR
MINTED_ACTOR = leg.MINTED_ACTOR


def argv(tamper=None):
    args = [
        "receipt_index_collision.py",
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
    or the exception collision_pass raises. Returns
    `(rc, stdout, stderr)`."""
    if isinstance(outcome, BaseException):
        stub = mock.Mock(side_effect=outcome)
    else:
        stub = mock.Mock(return_value=outcome)
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", argv(tamper)), \
            mock.patch.object(leg, "collision_pass", stub), \
            contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        rc = leg.main()
    return rc, out.getvalue(), err.getvalue()


def receipt(command=RACED, actor=RACED_ACTOR, outcome=None):
    return {
        "command": command,
        "actor": actor,
        "outcome": outcome if outcome is not None else {"accepted": {}},
    }


def entry(seq, tick, event):
    return {"seq": seq, "tick": tick, "event": event}


def settled_entry(seq, tick, command=RACED, actor=RACED_ACTOR,
                  outcome=None):
    return entry(
        seq,
        tick,
        {"command_settled": {"receipt": receipt(
            command, actor, outcome)}},
    )


def observed(raced_served, minted_served, raced_settles=1,
             minted_settles=1, raced_verdict="superseded",
             minted_verdict="applied@61481"):
    """The audit's observed record for a clean run: both admissions
    retrievable, one settle each per peer. The settle rows carry the
    same normalized verdict vocabulary the served receipts do."""
    def serves(values):
        return {name: value for name, value in zip(("duty", "standby"),
                                                   values)}

    def admission(values, count, verdict_name):
        return {
            "served": serves(values),
            "journaled": {
                name: [(index, index, verdict_name) for index
                       in range(count)]
                for name in ("duty", "standby")},
            "durable": {
                name: [(index, index, verdict_name) for index
                       in range(count)]
                for name in ("duty", "standby")},
        }

    return {
        "raced": admission(raced_served, raced_settles, raced_verdict),
        "minted": admission(minted_served, minted_settles, minted_verdict),
    }


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
        record = discovered["receipt_index_collision.py"]
        self.assertEqual(record["stem"], "receipt-index-collision")
        self.assertEqual(record["order"], 615)

    def test_the_named_diagnostics_follow_the_stem_convention(self):
        # The issue names receipt-collision-failed and
        # receipt-collision-nondeterministic. The nondeterministic name
        # is the driver's own `<stem>-nondeterministic` default off the
        # file stem; the failed name is not, so the literal declares it
        # — the leg's own collision vocabulary, not its file name's.
        self.assertEqual(leg.LEG["passes"], "receipt-index-collision")
        self.assertEqual(leg.LEG["failed"], "receipt-collision-failed")

    def test_the_doctored_case_carries_named_evidence(self):
        tampers = {entry["name"]: entry for entry in leg.LEG["tampers"]}
        self.assertEqual(set(tampers), {"expect-displaced"})
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
    """The receipt and journal projections the audit reads — each
    admission's submission identity, the verdict the collision audit
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

    def test_the_two_admissions_stay_distinct(self):
        # One absolute index, two commands and two actors: the audit
        # correlates each by its own submission record.
        self.assertTrue(leg.admission_hit(receipt(), RACED, RACED_ACTOR))
        self.assertFalse(
            leg.admission_hit(receipt(), MINTED, RACED_ACTOR))
        self.assertFalse(
            leg.admission_hit(receipt(), RACED, MINTED_ACTOR))
        self.assertTrue(
            leg.admission_hit(
                receipt(MINTED, MINTED_ACTOR), MINTED, MINTED_ACTOR))

    def test_admission_receipts_filters_by_submission(self):
        receipts = [
            receipt(),
            receipt(MINTED, MINTED_ACTOR),
            receipt(actor="someone-else"),
        ]
        self.assertEqual(
            leg.admission_receipts(receipts, RACED, RACED_ACTOR),
            [receipts[0]],
        )
        self.assertEqual(
            leg.admission_receipts(receipts, MINTED, MINTED_ACTOR),
            [receipts[1]],
        )

    def test_settle_rows_projects_in_seq_order(self):
        entries = [
            settled_entry(7, 40, outcome={"applied": {"tick": 40}}),
            entry(8, 41, {"point_changed": {"point": POINT}}),
            settled_entry(9, 42, outcome={"applied": {"tick": 40}}),
        ]
        self.assertEqual(
            leg.settle_rows(entries, RACED, RACED_ACTOR),
            [(7, 40, "applied@40"), (9, 42, "applied@40")],
        )

    def test_settle_rows_ignores_the_other_admission(self):
        entries = [settled_entry(
            7, 40, MINTED, MINTED_ACTOR, outcome={"applied": {"tick": 40}})]
        self.assertEqual(leg.settle_rows(entries, RACED, RACED_ACTOR), [])

    def test_journal_entries_reads_the_durable_record(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "journal.jsonl"
            path.write_text(
                json.dumps({"run_boundary": {"run": 1, "tick": 0}})
                + "\n"
                + json.dumps({"entry": settled_entry(
                    3, 40, outcome={"applied": {"tick": 40}})})
                + "\n"
            )
            entries = leg.journal_entries(str(path))
            self.assertEqual(
                leg.settle_rows(entries, RACED, RACED_ACTOR),
                [(3, 40, "applied@40")],
            )


class CollisionAudit(unittest.TestCase):
    """The audit's own decision: every admitted command keeps a
    retrievable terminal verdict, one settle per admission per peer,
    and the two submissions stand as distinct records — or the named
    fault the run reports. The doctored negative the leg's own tamper
    plants is the first case here."""

    def test_a_preserved_collision_carries_no_fault(self):
        self.assertIsNone(leg.collision_fault(
            observed(("superseded", "superseded"),
                     ("applied@61481", "applied@61481")),
            None,
        ))

    def test_the_displaced_admission_is_failed(self):
        # The finding's shape: only the successor's record stands at
        # the contested index and the raced admission is nowhere.
        fault = leg.collision_fault(
            observed((None, None), ("applied@61481", "applied@61481"),
                     raced_settles=0, minted_settles=1),
            None,
        )
        self.assertEqual(fault[0], "receipt-collision-failed")
        self.assertIn("silently replaced", fault[1])

    def test_a_doubled_settle_is_nondeterministic(self):
        fault = leg.collision_fault(
            observed(("superseded", "superseded"),
                     ("applied@61481", "applied@61481"),
                     raced_settles=2),
            None,
        )
        self.assertEqual(fault[0], "receipt-collision-nondeterministic")
        self.assertIn("records the raced admission 2 times", fault[1])

    def test_two_verdicts_for_one_admission_is_nondeterministic(self):
        # One peer's served receipt and both peers' journals read two
        # different verdicts for the same admission — a cross-surface
        # instability, not the one peer's two views of one settle.
        record = observed(("superseded", "applied@61481"),
                          ("applied@61481", "applied@61481"))
        record["raced"]["journaled"]["standby"] = [
            (7, 7, "applied@61481")]
        record["raced"]["durable"]["standby"] = [(7, 7, "applied@61481")]
        fault = leg.collision_fault(record, None)
        self.assertEqual(fault[0], "receipt-collision-nondeterministic")
        self.assertIn("not one verdict", fault[1])

    def test_the_collapsed_record_is_nondeterministic(self):
        # One peer serves the displaced admission's record where the
        # colliding successor's own admission has none — the two
        # submissions standing on one record.
        fault = leg.collision_fault(
            observed(("superseded", "superseded"),
                     ("applied@61481", None)),
            None,
        )
        self.assertEqual(fault[0], "receipt-collision-nondeterministic")
        self.assertIn("collapsed onto one record", fault[1])

    def test_the_doctored_case_is_reported_on_the_honest_record(self):
        fault = leg.collision_fault(
            observed(("superseded", "superseded"),
                     ("applied@61481", "applied@61481")),
            "expect-displaced",
        )
        self.assertEqual(fault[0], "receipt-collision-failed")
        self.assertIn("accepted the displaced admission", fault[1])

    def test_a_durable_view_disagreeing_with_its_own_served_journal_is_failed(
        self,
    ):
        # The file is the served journal's sink, so one peer's two views
        # of one settle carry the same verdict. The recorded contract
        # names this clause receipt-collision-failed rather than the
        # cross-surface nondeterministic read below.
        record = observed(("superseded", "superseded"),
                          ("applied@61481", "applied@61481"))
        record["raced"]["durable"]["duty"] = [(9, 6, "applied@61481")]
        fault = leg.collision_fault(record, None)
        self.assertEqual(fault[0], "receipt-collision-failed")
        self.assertIn("the durable audit is not the served audit", fault[1])


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
            "receipts": [receipt(), receipt()],
            "command_admission": {"attempts": 4},
        }
        receipts, base = self.surface(checkpoint)
        self.assertEqual(len(receipts), 2)
        self.assertEqual(base, 2)
        # The split mint is only stageable when both lines mint the
        # same index, which the high-water is what decides.
        self.assertEqual(base + len(receipts), 4)

    def test_each_missing_field_is_inconclusive(self):
        for checkpoint in (
            {"command_admission": {"attempts": 4}},
            {"receipts": []},
            {"receipts": [], "command_admission": {}},
            {"receipts": [], "command_admission": {"attempts": "4"}},
        ):
            with self.assertRaises(leg.Inconclusive, msg=checkpoint):
                self.surface(checkpoint)


class Inconclusive(unittest.TestCase):
    """The release-precedence classification: a pinned release
    predating the receipt-index-collision contract raises the leg's
    Inconclusive, which main renders as a stable digest line and a
    zero exit — and a doctored case over an inconclusive run still
    fails, since it can name no evidence."""

    def test_main_reports_the_inconclusive_digest(self):
        rc, out, err = run_main(
            outcome=leg.Inconclusive(
                "the pinned release predates the receipt-index "
                "collision contract"
            )
        )
        self.assertEqual(rc, 0)
        self.assertIn(
            "receipt-collision-digest inconclusive — the pinned "
            "release predates",
            out,
        )
        self.assertIn("receipt-collision: inconclusive", err)

    def test_two_inconclusive_passes_render_identically(self):
        first = run_main(outcome=leg.Inconclusive("pre-contract"))
        second = run_main(outcome=leg.Inconclusive("pre-contract"))
        self.assertEqual(first[0], 0)
        self.assertEqual(first[1], second[1])

    def test_a_doctored_case_over_an_inconclusive_run_fails(self):
        rc, out, err = run_main(
            tamper="expect-displaced",
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
            outcome=([], {}, ["the raced admission was displaced"])
        )
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn(
            "receipt-collision: the raced admission was displaced", err
        )

    def test_an_abort_reports_by_name(self):
        rc, out, err = run_main(
            outcome=leg.Abort("the pair never converged")
        )
        self.assertEqual(rc, 1)
        self.assertIn("receipt-collision: the pair never converged", err)

    def test_a_doctored_case_never_passes_silently(self):
        rc, out, err = run_main(
            tamper="expect-displaced", outcome=([], {}, [])
        )
        self.assertEqual(rc, 1)
        self.assertIn("passed silently", err)

    def test_a_doctored_case_carrying_failures_still_fails(self):
        rc, _, err = run_main(
            tamper="expect-displaced",
            outcome=([], {}, ["the doctored expectation accepted the "
                              "displaced admission"]),
        )
        self.assertEqual(rc, 1)
        self.assertIn("accepted the displaced admission", err)

    def test_a_clean_pass_renders_the_digest_line(self):
        rc, out, err = run_main(
            outcome=([{"phase": "converge"},
                      {"phase": "mint", "index": 4},
                      {"phase": "audit"}], {}, [])
        )
        self.assertEqual(rc, 0, err)
        self.assertIn("receipt-collision-digest ", out)
        self.assertIn("one command_settled per admission per peer "
                      "journal", out)
        self.assertEqual(err, "")


if __name__ == "__main__":
    unittest.main()