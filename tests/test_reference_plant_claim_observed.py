"""The claim_observed leg's unit coverage — ci/legs/
claim_observed.py is the reference plant's consumer-boundary mirror
of the qa rig's claim-observation scenario (#1007's
2360_claim_observation.py), proving #987's observed-owner journal
contract on the released pair: one attributed field_claim_observed
per observed owner token on the observing peer — never one per
refused probe — beside the unchanged loss, reclaim, and role-change
entries, with the same record landing in seq order in the
manifest-declared durable journal file. These tests pin, without
launching the pair: the leg's registration record, its journal
projections, and the inconclusive / doctored-case classifications
the harness relies on — the new leg's evidence lines and
inconclusive handling asserted as the issue requires."""
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
_LEG_PATH = _CI_DIR / "legs" / "claim_observed.py"
_SCENARIO_PATH = _CI_DIR / "scenario.json"
_MANIFEST_PATH = _ROOT / "reference-plant" / "deploy" / "manifest.json"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


legs = load(_CI_DIR / "legs.py", "legs")
leg = load(_LEG_PATH, "claim_observed")


def argv(tamper=None):
    args = [
        "claim_observed.py",
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
    or the exception claim_observed_pass raises. Returns
    `(rc, stdout, stderr)`."""
    if isinstance(outcome, BaseException):
        stub = mock.Mock(side_effect=outcome)
    else:
        stub = mock.Mock(return_value=outcome)
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", argv(tamper)), \
            mock.patch.object(leg, "claim_observed_pass", stub), \
            contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        rc = leg.main()
    return rc, out.getvalue(), err.getvalue()


def entry(seq, tick, event):
    return {"seq": seq, "tick": tick, "event": event}


class Registration(unittest.TestCase):
    """The leg's `LEG` literal — the pair stage's discovery contract:
    the declared order is unique across the directory, the stem and
    the named diagnostics follow the file-name convention, and every
    doctored case declares the evidence the honest run reports."""

    def test_the_leg_registers_in_the_pair_stage(self):
        discovered = {
            Path(record["file"]).name: record
            for record in legs.discover(str(_CI_DIR / "legs"))
        }
        record = discovered["claim_observed.py"]
        self.assertEqual(record["stem"], "claim-observed")
        self.assertEqual(record["order"], 380)

    def test_the_named_diagnostics_follow_the_stem_convention(self):
        # The issue names claim-observed-failed and
        # claim-observed-nondeterministic — the driver's defaults off
        # the file stem, undeclared in the literal.
        self.assertNotIn("failed", leg.LEG)
        self.assertEqual(leg.LEG["passes"], "claim-observed")

    def test_the_doctored_case_carries_named_evidence(self):
        tampers = {entry["name"]: entry for entry in leg.LEG["tampers"]}
        self.assertEqual(set(tampers), {"expect-silence"})
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
    """The journal projections the audit reads — the observed and
    lost records as (seq, record) pairs and the role walk as
    (from, to, origin) triples in seq order."""

    def test_observed_entries_project_in_seq_order(self):
        entries = [
            entry(3, 7, {"field_claim_lost": {"point": 5, "claimant": 9}}),
            entry(4, 8, {"field_claim_observed": {"point": 5, "claimant": 11}}),
            entry(5, 8, {"point_changed": {"point": 5, "to": True}}),
            entry(6, 9, {"field_claim_observed": {"point": 5, "claimant": 13}}),
        ]
        self.assertEqual(
            leg.observed_entries(entries),
            [
                (4, {"point": 5, "claimant": 11}),
                (6, {"point": 5, "claimant": 13}),
            ],
        )

    def test_lost_entries_project_in_seq_order(self):
        entries = [
            entry(3, 7, {"field_claim_lost": {"point": 5, "claimant": 9}}),
            entry(4, 8, {"role_changed": {"from": "active", "to": "demoting"}}),
        ]
        self.assertEqual(
            leg.lost_entries(entries),
            [(3, {"point": 5, "claimant": 9})],
        )

    def test_the_role_walk_carries_the_switch_origins(self):
        entries = [
            entry(3, 7, {"role_changed": {
                "from": "active", "to": "demoting", "origin": "fenced"}}),
            entry(4, 8, {"role_changed": {
                "from": "demoting", "to": "standby", "origin": "fenced"}}),
            entry(9, 11, {"role_changed": {
                "from": "standby", "to": "promoting",
                "origin": "reclaim"}}),
            entry(10, 12, {"role_changed": {
                "from": "promoting", "to": "active",
                "origin": "reclaim"}}),
        ]
        self.assertEqual(leg.role_walk(entries), leg.ROLE_WALK)

    def test_an_entry_predating_attribution_walks_with_no_origin(self):
        entries = [
            entry(3, 7, {"role_changed": {"from": "active", "to": "demoting"}}),
        ]
        self.assertEqual(
            leg.role_walk(entries), [("active", "demoting", None)]
        )

    def test_the_verdict_owner_reads_the_fencing_attribution(self):
        verdict = {
            "result": "error",
            "error": {
                "kind": "fenced",
                "detail": "another attachment owns field writes",
                "owner": 4242,
            },
        }
        self.assertEqual(leg.verdict_owner(verdict), 4242)
        self.assertIsNone(leg.verdict_owner({"result": "done"}))
        self.assertIsNone(leg.verdict_owner(None))
        self.assertIsNone(
            leg.verdict_owner(
                {"result": "error", "error": {"kind": "unclaimed"}}
            )
        )


class Inconclusive(unittest.TestCase):
    """The release-precedence classification: a pinned release
    predating the observed-claimant contract raises the leg's
    Inconclusive, which main renders as a stable digest line and a
    zero exit — and a doctored case over an inconclusive run still
    fails, since it can name no evidence."""

    def test_main_reports_the_inconclusive_digest(self):
        rc, out, err = run_main(
            outcome=leg.Inconclusive(
                "the pinned release predates the observed-claimant "
                "contract"
            )
        )
        self.assertEqual(rc, 0)
        self.assertIn(
            "claim-observed-digest inconclusive — the pinned release "
            "predates",
            out,
        )
        self.assertIn("claim-observed: inconclusive", err)

    def test_two_inconclusive_passes_render_identically(self):
        first = run_main(outcome=leg.Inconclusive("pre-contract"))
        second = run_main(outcome=leg.Inconclusive("pre-contract"))
        self.assertEqual(first[0], 0)
        self.assertEqual(first[1], second[1])

    def test_a_doctored_case_over_an_inconclusive_run_fails(self):
        rc, out, err = run_main(
            tamper="expect-silence",
            outcome=leg.Inconclusive("pre-contract"),
        )
        self.assertEqual(rc, 1)
        self.assertIn(
            "the doctored expectation wanted the observed claimant "
            "unrecorded",
            err,
        )


class Classification(unittest.TestCase):
    """main()'s exit classification over the pass result — failures
    stream to stderr prefixed by the stem, a doctored case may never
    exit zero, and a clean pass renders the sha digest line."""

    def test_failures_report_by_name(self):
        rc, out, err = run_main(
            outcome=([], {}, ["the observed record duplicated"])
        )
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn(
            "claim-observed: the observed record duplicated", err
        )

    def test_an_abort_reports_by_name(self):
        rc, out, err = run_main(
            outcome=leg.Abort("the pair never converged")
        )
        self.assertEqual(rc, 1)
        self.assertIn(
            "claim-observed: the pair never converged", err
        )

    def test_a_doctored_case_never_passes_silently(self):
        rc, out, err = run_main(
            tamper="expect-silence", outcome=([], {}, [])
        )
        self.assertEqual(rc, 1)
        self.assertIn("passed silently", err)

    def test_a_doctored_case_carrying_failures_still_fails(self):
        rc, _, _ = run_main(
            tamper="expect-silence", outcome=([], {}, ["named evidence"])
        )
        self.assertEqual(rc, 1)

    def test_a_clean_pass_renders_the_digest_line(self):
        evidence = {
            "converged": 4,
            "observed_claimant": leg.CLAIM_OBSERVED,
            "observed_seq": 309,
            "restored_at": 18,
        }
        rc, out, err = run_main(
            outcome=([{"phase": "converge"}], evidence, [])
        )
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"^claim-observed-digest [0-9a-f]{64} — ")
        self.assertIn("tracking by tick 4", out)
        self.assertIn(hex(leg.CLAIM_OBSERVED), out)
        self.assertIn("restored at tick 18", out)


if __name__ == "__main__":
    unittest.main()
