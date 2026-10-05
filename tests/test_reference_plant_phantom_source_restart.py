"""The phantom_source_restart leg's unit coverage — ci/legs/
phantom_source_restart.py is the reference plant's consumer-boundary
mirror of the qa rig's phantom-source-restart leg (#1132's leg beside
#1133's), proving the same-generation source_restarted suppression
contract on the released pair. These tests pin, without launching the
pair: the leg's registration record and its doctored cases, the
journal and stream-position projections its audit reads, the phantom
and genuine-restart classifications the contract and the harness rely
on, and the inconclusive / doctored-case handling the harness
classifies by — the new leg's evidence lines asserted as the issue
requires."""
import contextlib
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
_CI_DIR = _ROOT / "reference-plant" / "ci"
_LEG_PATH = _CI_DIR / "legs" / "phantom_source_restart.py"
_SCENARIO_PATH = _CI_DIR / "scenario.json"
_MANIFEST_PATH = _ROOT / "reference-plant" / "deploy" / "manifest.json"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


legs = load(_CI_DIR / "legs.py", "legs")
sys.path.insert(0, str(_CI_DIR / "legs"))
leg = load(_LEG_PATH, "phantom_source_restart")


def argv(tamper=None):
    args = [
        "phantom_source_restart.py",
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
    the exception the pass raises. Returns `(rc, stdout, stderr)`."""
    if isinstance(outcome, BaseException):
        stub = mock.Mock(side_effect=outcome)
    else:
        stub = mock.Mock(return_value=outcome)
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", argv(tamper)), \
            mock.patch.object(leg, "phantom_source_restart_pass", stub), \
            contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        rc = leg.main()
    return rc, out.getvalue(), err.getvalue()


def entry(seq, tick, kind, body=None):
    return {"seq": seq, "tick": tick, "event": {kind: body or {}}}


def restart_entry(seq, tick, was_aligned, resumed_at):
    return entry(
        seq,
        tick,
        "source_restarted",
        {"was_aligned": was_aligned, "resumed_at": resumed_at},
    )


class Registration(unittest.TestCase):
    """The leg's `LEG` literal — the pair stage's discovery contract:
    the declared order is unique across the directory, the stem and the
    named diagnostics follow the file-name convention, and every
    doctored case declares the evidence the honest run reports."""

    def test_the_leg_registers_in_the_pair_stage(self):
        discovered = {
            Path(record["file"]).name: record
            for record in legs.discover(str(_CI_DIR / "legs"))
        }
        record = discovered["phantom_source_restart.py"]
        self.assertEqual(record["stem"], "phantom-source-restart")
        self.assertEqual(record["order"], 605)
        # The issue names source-restart-evidence-failed; the driver's
        # default off the file stem is overridden in the literal.
        self.assertEqual(
            record["failed"], "source-restart-evidence-failed"
        )
        self.assertEqual(record["passes"], "phantom-source-restart")

    def test_the_leg_sorts_between_its_recorded_neighbours(self):
        discovered = [
            Path(record["file"]).name
            for record in legs.discover(str(_CI_DIR / "legs"))
        ]
        index = discovered.index("phantom_source_restart.py")
        self.assertEqual(
            discovered[index - 1], "tracker_realign_tick_order.py"
        )
        self.assertEqual(
            discovered[index + 1], "foreign_claim_release.py"
        )

    def test_the_doctored_cases_carry_named_evidence(self):
        tampers = {entry["name"]: entry for entry in leg.LEG["tampers"]}
        self.assertEqual(
            set(tampers), {"planted-phantom", "missing-restart"}
        )
        # The declared evidence must be a diagnostic the leg actually
        # prints — pin it against the source's failure lines so a
        # drifted message cannot pass the harness's substring check.
        source = _LEG_PATH.read_text()
        for name, record in tampers.items():
            self.assertTrue(record["evidence"], name)
            for evidence in record["evidence"]:
                self.assertIn(evidence, source, name)
            for field in ("passed", "missed"):
                self.assertIsInstance(record[field], str)

    def test_the_leg_rides_the_pair_harness_rather_than_forking_it(self):
        # The launch/settle/restore harness is the pair stage's shared
        # seam; the leg's staging binds it, never a private copy.
        self.assertIs(leg.pair.Abort, leg.Abort)
        for helper in ("launch_pair", "manifest_pair", "get", "scan",
                       "stop", "spawn_peer", "journal_records"):
            self.assertTrue(
                hasattr(leg.pair, helper), helper
            )
        self.assertGreater(leg.CYCLE_SCANS, 0)
        self.assertGreater(leg.RESTART_SCANS, 0)
        self.assertGreater(leg.RESTORE_SCANS, 0)


class Projections(unittest.TestCase):
    """The journal and checkpoint projections the audit reads."""

    def test_restart_entries_project_in_seq_order(self):
        entries = [
            entry(3, 7, "role_changed",
                  {"from": "active", "to": "demoting"}),
            restart_entry(4, 8, 122, 1),
            entry(5, 9, "point_changed", {"point": 10, "to": True}),
            restart_entry(6, 10, 124, 2),
        ]
        self.assertEqual(
            leg.restarts(entries),
            [(4, 8, 122, 1), (6, 10, 124, 2)],
        )

    def test_a_journal_without_restarts_projects_nothing(self):
        self.assertEqual(
            leg.restarts([entry(1, 3, "role_changed",
                                {"from": "standby", "to": "promoting"})]),
            [],
        )

    def test_the_stream_position_prefers_the_declared_lead(self):
        self.assertEqual(
            leg.stream_position({"tick": 130, "stream_tick": 118}), 118
        )

    def test_a_lead_free_document_claims_its_run_tick(self):
        self.assertEqual(leg.stream_position({"tick": 130}), 130)

    def test_a_document_without_an_integer_tick_projects_none(self):
        self.assertIsNone(leg.stream_position({"stream_tick": 118}))
        self.assertIsNone(leg.stream_position({"tick": None}))
        self.assertIsNone(leg.stream_position("not a document"))

    def test_the_contract_surface_requires_both_stamps(self):
        self.assertTrue(
            leg.contract_checkpoint({"tick": 12, "generation": 9})
        )
        # A release predating the generation stamp cannot be judged.
        self.assertFalse(leg.contract_checkpoint({"tick": 12}))
        self.assertFalse(
            leg.contract_checkpoint({"generation": 9})
        )

    def test_the_tracking_posture_is_read_off_the_role_report(self):
        self.assertTrue(
            leg.tracking({"role": "standby",
                          "sync": {"tracking": {"aligned": 12}}})
        )
        self.assertFalse(
            leg.tracking({"role": "standby", "sync": "unsynchronized"})
        )
        self.assertFalse(leg.tracking({"role": "active"}))

    def test_the_alignment_read_ignores_a_non_integer_mark(self):
        self.assertEqual(
            leg.aligned_tick({"sync": {"tracking": {"aligned": 12}}}), 12
        )
        self.assertIsNone(
            leg.aligned_tick({"sync": {"tracking": {"aligned": None}}})
        )
        self.assertIsNone(leg.aligned_tick({"role": "active"}))


class PhantomAudit(unittest.TestCase):
    """The same-generation half: an uninterrupted checkpoint stream
    journals no restart on either the serving monitor or the durable
    file, and the served stream position stays monotone while a
    tracking run realigns its own tick underneath the line."""

    def observed(self, **overrides):
        record = {
            "rows": [{"role": "standby", "sync": "tracking",
                      "aligned": 117, "tick": 131}],
            "served": [100, 101, 102],
            "served_phantom": [],
            "durable_phantom": [],
        }
        record.update(overrides)
        return record

    def test_an_uninterrupted_stream_passes(self):
        failures = []
        leg.audit_phantom("the demoted peer", self.observed(), failures)
        self.assertEqual(failures, [])

    def test_a_phantom_on_the_serving_monitor_fails(self):
        failures = []
        leg.audit_phantom(
            "the demoted ctrl-a",
            self.observed(served_phantom=[(None, 131, None, 130)]),
            failures,
        )
        self.assertEqual(len(failures), 1)
        self.assertIn("serving monitor", failures[0])
        # The wording the leg's planted-phantom tamper evidence is
        # pinned against, so a drifted message cannot pass the
        # harness's substring check by accident.
        self.assertIn(
            "a phantom restart the demote-to-track reset manufactures",
            failures[0],
        )

    def test_a_phantom_in_the_durable_journal_fails(self):
        failures = []
        leg.audit_phantom(
            "the demoted peer",
            self.observed(durable_phantom=[(9, 131, None, 130)]),
            failures,
        )
        self.assertEqual(len(failures), 1)
        self.assertIn("durable journal", failures[0])

    def test_a_non_monotone_served_stream_fails(self):
        failures = []
        leg.audit_phantom(
            "the demoted peer",
            self.observed(served=[100, 104, 103]),
            failures,
        )
        self.assertEqual(len(failures), 1)
        self.assertIn("non-monotone", failures[0])


class RestartAudit(unittest.TestCase):
    """The genuine-restart half: a cold-restarted source journals
    exactly one entry on its tracking peer, served and durable alike,
    carrying the prior alignment it broke and the resumed tick below
    it."""

    def test_the_one_named_entry_passes(self):
        failures = []
        leg.audit_restart(
            "ctrl-b",
            [(11, 141, 122, 1)],
            [(11, 141, 122, 1)],
            122,
            failures,
        )
        self.assertEqual(failures, [])

    def test_an_absent_entry_fails(self):
        failures = []
        leg.audit_restart("ctrl-b", [], [], 122, failures)
        self.assertEqual(len(failures), 1)
        # The wording the leg's missing-restart tamper evidence is
        # pinned against.
        self.assertIn(
            "a genuinely cold-restarted source journals exactly one",
            failures[0],
        )

    def test_a_duplicated_entry_fails(self):
        failures = []
        leg.audit_restart(
            "ctrl-b",
            [(11, 141, 122, 1), (12, 142, 123, 2)],
            [(11, 141, 122, 1), (12, 142, 123, 2)],
            122,
            failures,
        )
        self.assertEqual(len(failures), 1)
        self.assertIn("exactly one", failures[0])

    def test_an_entry_without_its_evidence_fails(self):
        for was_aligned, resumed_at in ((None, 1), (122, None)):
            failures = []
            leg.audit_restart(
                "ctrl-b",
                [(11, 141, was_aligned, resumed_at)],
                [(11, 141, was_aligned, resumed_at)],
                122,
                failures,
            )
            # One finding per side — the served journal and the
            # durable file carry the same malformed record.
            self.assertEqual(len(failures), 2, (was_aligned, resumed_at))
            self.assertIn("names no", failures[0])
            self.assertIn("durable journal", failures[1])

    def test_a_non_regressing_resume_fails(self):
        failures = []
        leg.audit_restart(
            "ctrl-b",
            [(11, 141, 122, 130)],
            [(11, 141, 122, 130)],
            122,
            failures,
        )
        self.assertEqual(len(failures), 2)
        self.assertIn("never regressed", failures[0])
        self.assertIn("durable journal", failures[1])

    def test_an_entry_naming_the_wrong_alignment_fails(self):
        failures = []
        leg.audit_restart(
            "ctrl-b",
            [(11, 141, 99, 1)],
            [(11, 141, 99, 1)],
            122,
            failures,
        )
        self.assertEqual(len(failures), 2)
        self.assertIn("pre-restart stream", failures[0])
        self.assertIn("durable journal", failures[1])

    def test_served_and_durable_disagreement_fails(self):
        failures = []
        leg.audit_restart(
            "ctrl-b",
            [(11, 141, 122, 1)],
            [(12, 141, 122, 1)],
            122,
            failures,
        )
        self.assertEqual(len(failures), 1)
        self.assertIn("durable audit", failures[0])


class Inconclusive(unittest.TestCase):
    """The release-precedence classification: a pinned release
    predating the generation stamp raises the leg's Inconclusive,
    which the pair stage renders as a stable digest line and a zero
    exit — and a doctored case over an inconclusive run still fails,
    since it can name no evidence."""

    def test_main_reports_the_inconclusive_digest(self):
        rc, out, err = run_main(
            outcome=leg.Inconclusive(
                "the pinned release predates the same-generation "
                "suppression contract"
            )
        )
        self.assertEqual(rc, 0)
        self.assertIn(
            "phantom-source-restart-digest inconclusive — the pinned "
            "release predates",
            out,
        )
        self.assertIn("phantom-source-restart: inconclusive", err)

    def test_two_inconclusive_passes_render_identically(self):
        first = run_main(outcome=leg.Inconclusive("pre-contract"))
        second = run_main(outcome=leg.Inconclusive("pre-contract"))
        self.assertEqual(first[0], 0)
        self.assertEqual(first[1], second[1])

    def test_a_doctored_case_over_an_inconclusive_run_fails(self):
        rc, out, err = run_main(
            tamper="planted-phantom",
            outcome=leg.Inconclusive("pre-contract"),
        )
        self.assertEqual(rc, 1)
        self.assertIn(
            "the planted-phantom case wanted the run to surface its "
            "named diagnostic",
            err,
        )


class Classification(unittest.TestCase):
    """main()'s exit classification over the pass result — failures
    stream to stderr prefixed by the stem, a doctored case may never
    exit zero, and a clean pass renders the sha digest line."""

    def test_failures_report_by_name(self):
        rc, out, err = run_main(
            outcome=([], {}, ["the phantom entry was never journaled"])
        )
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn(
            "phantom-source-restart: the phantom entry was never "
            "journaled",
            err,
        )

    def test_an_abort_reports_by_name(self):
        rc, out, err = run_main(
            outcome=leg.Abort("the pair never converged")
        )
        self.assertEqual(rc, 1)
        self.assertIn(
            "phantom-source-restart: the pair never converged", err
        )

    def test_a_doctored_case_never_passes_silently(self):
        rc, out, err = run_main(
            tamper="planted-phantom", outcome=([], {}, [])
        )
        self.assertEqual(rc, 1)
        self.assertIn("passed silently", err)

    def test_a_doctored_case_carrying_failures_still_fails(self):
        rc, _, _ = run_main(
            tamper="missing-restart",
            outcome=([], {}, ["named evidence"]),
        )
        self.assertEqual(rc, 1)

    def test_a_clean_pass_renders_the_digest_line(self):
        evidence = {
            "converged": 4,
            "aligned_before": 122,
            "resumed_at": 1,
            "restart": {"served": [(11, 141, 122, 1)],
                        "durable": [(11, 141, 122, 1)]},
        }
        rc, out, err = run_main(
            outcome=([{"phase": "converge"}], evidence, [])
        )
        self.assertEqual(rc, 0)
        self.assertRegex(
            out, r"^phantom-source-restart-digest [0-9a-f]{64} — "
        )
        self.assertIn("journalled no source_restart", out)
        self.assertIn("exactly one source_restarted", out)


if __name__ == "__main__":
    unittest.main()