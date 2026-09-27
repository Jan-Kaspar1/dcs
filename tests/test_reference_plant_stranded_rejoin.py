"""The stranded_rejoin leg's unit coverage — ci/legs/
stranded_rejoin.py is the reference plant's consumer-boundary
mirror of the qa rig's stranded-standby scenario
(`2370_stranded_standby_no_resync`), proving decision 101's
claim-declared-monitor re-join contract on the released pair:
`POST /promote` on the tracking standby with no `POST /demote`
on the owner first demotes the superseded owner in place, whose
fenced write resolves the standing claim's declared monitor,
journals the attributed `field_claim_lost` and
`tracking_source_adopted`, and reports `tracking` inside the
bound — with the #1045 monitor-less window exercising the
un-converged hold. These tests pin, without launching the pair:
the leg's registration record, its journal/verdict projections,
and the inconclusive / doctored-case classifications the harness
relies on — the new leg's evidence lines and inconclusive
handling asserted as the issue requires."""
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
_LEG_PATH = _CI_DIR / "legs" / "stranded_rejoin.py"
_SCENARIO_PATH = _CI_DIR / "scenario.json"
_MANIFEST_PATH = _ROOT / "reference-plant" / "deploy" / "manifest.json"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


legs = load(_CI_DIR / "legs.py", "legs")
leg = load(_LEG_PATH, "stranded_rejoin")


def argv(tamper=None):
    args = [
        "stranded_rejoin.py",
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
    or the exception stranded_rejoin_pass raises. Returns
    `(rc, stdout, stderr)`."""
    if isinstance(outcome, BaseException):
        stub = mock.Mock(side_effect=outcome)
    else:
        stub = mock.Mock(return_value=outcome)
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", argv(tamper)), \
            mock.patch.object(leg, "stranded_rejoin_pass", stub), \
            contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        rc = leg.main()
    return rc, out.getvalue(), err.getvalue()


def entry(seq, tick, event):
    return {"seq": seq, "tick": tick, "event": event}


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
        record = discovered["stranded_rejoin.py"]
        self.assertEqual(record["stem"], "stranded-rejoin")
        self.assertEqual(record["order"], 420)

    def test_the_named_diagnostics_follow_the_stem_convention(self):
        # The issue names stranded-rejoin-failed and
        # stranded-rejoin-nondeterministic — the driver's defaults
        # off the file stem, undeclared in the literal.
        self.assertNotIn("failed", leg.LEG)
        self.assertEqual(leg.LEG["passes"], "stranded-rejoin-leg")

    def test_the_doctored_case_carries_named_evidence(self):
        tampers = {entry["name"]: entry for entry in leg.LEG["tampers"]}
        self.assertEqual(set(tampers), {"expect-wedge"})
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
    """The journal and verdict projections the audit reads — the
    loss and adoption records as (seq, record) pairs, the role
    walk as (from, to, origin) triples in seq order, the sync
    vocabulary read off a report, and the declared-monitor port
    the fencing verdict names."""

    def test_lost_entries_project_in_seq_order(self):
        entries = [
            entry(3, 7, {"field_claim_lost": {"point": 5, "claimant": 9}}),
            entry(4, 8, {"role_changed": {"from": "active", "to": "demoting"}}),
            entry(5, 8, {"field_claim_lost": {"point": 5, "claimant": 11}}),
        ]
        self.assertEqual(
            leg.lost_entries(entries),
            [(3, {"point": 5, "claimant": 9}),
             (5, {"point": 5, "claimant": 11})],
        )

    def test_adopted_entries_project_in_seq_order(self):
        entries = [
            entry(3, 7, {"tracking_source_adopted": {
                "source": "127.0.0.1:9001"}}),
            entry(4, 8, {"role_changed": {"from": "demoting", "to": "standby"}}),
            entry(5, 9, {"tracking_source_adopted": {
                "source": "127.0.0.1:9002"}}),
        ]
        self.assertEqual(
            leg.adopted_entries(entries),
            [(3, {"source": "127.0.0.1:9001"}),
             (5, {"source": "127.0.0.1:9002"})],
        )

    def test_the_role_walk_carries_the_switch_origins(self):
        entries = [
            entry(3, 7, {"role_changed": {
                "from": "active", "to": "demoting", "origin": "fenced"}}),
            entry(4, 8, {"role_changed": {
                "from": "demoting", "to": "standby", "origin": "fenced"}}),
        ]
        self.assertEqual(
            leg.role_walk(entries),
            [("active", "demoting", "fenced"),
             ("demoting", "standby", "fenced")],
        )

    def test_an_entry_predating_attribution_walks_with_no_origin(self):
        entries = [
            entry(3, 7, {"role_changed": {"from": "active", "to": "demoting"}}),
        ]
        self.assertEqual(
            leg.role_walk(entries), [("active", "demoting", None)]
        )

    def test_the_demote_walk_is_the_fenced_in_place_walk(self):
        entries = [
            entry(3, 7, {"role_changed": {
                "from": "active", "to": "demoting", "origin": "fenced"}}),
            entry(4, 8, {"role_changed": {
                "from": "demoting", "to": "standby", "origin": "fenced"}}),
        ]
        self.assertTrue(leg.demote_walk(entries))

    def test_the_promote_walk_is_the_request_origin_walk(self):
        entries = [
            entry(3, 7, {"role_changed": {
                "from": "standby", "to": "promoting",
                "origin": "request"}}),
            entry(4, 8, {"role_changed": {
                "from": "promoting", "to": "active",
                "origin": "request"}}),
        ]
        self.assertTrue(leg.promote_walk(entries))

    def test_sync_kind_reads_the_string_and_variant_forms(self):
        self.assertEqual(
            leg.sync_kind({"sync": "unsynchronized"}), "unsynchronized")
        self.assertEqual(
            leg.sync_kind({"sync": {"tracking": {"aligned": 12}}}),
            "tracking",
        )
        self.assertEqual(leg.sync_kind({"role": "active"}), "missing")

    def test_monitor_port_reads_the_endpoint_suffix(self):
        self.assertEqual(leg.monitor_port("http://127.0.0.1:9001"), "9001")
        self.assertEqual(leg.monitor_port("127.0.0.1:8080"), "8080")

    def test_verdict_monitor_reads_the_declared_monitor(self):
        verdict = {"error": {"kind": "fenced", "owner": 7,
                             "monitor": "127.0.0.1:9001"}}
        self.assertEqual(leg.verdict_monitor(verdict), "127.0.0.1:9001")
        self.assertIsNone(leg.verdict_monitor(
            {"error": {"kind": "fenced", "owner": 7}}))
        self.assertIsNone(leg.verdict_monitor({"result": "done"}))

    def test_the_leg_reuses_the_claim_reclaim_staging(self):
        # The sibling mirrored legs stage claim evidence through
        # claim_reclaim's helpers — the verdict classification and
        # the tracking check resolve there, never a private copy.
        claim_reclaim = leg.claim_reclaim
        verdict = {
            "result": "error",
            "error": {
                "kind": "fenced",
                "detail": "another attachment owns field writes",
                "owner": 4242,
            },
        }
        self.assertTrue(claim_reclaim.mutation_fenced(verdict))
        self.assertEqual(claim_reclaim.verdict_owner(verdict), 4242)
        for helper in (
            claim_reclaim.mutation_fenced,
            claim_reclaim.verdict_owner,
            claim_reclaim.tracking,
            claim_reclaim.sync_state,
        ):
            self.assertEqual(helper.__module__, "claim_reclaim")
        self.assertNotEqual(leg.TOOL_CLAIM, claim_reclaim.FOREIGN_OWNER)


class Inconclusive(unittest.TestCase):
    """The release-precedence classification: a pinned release
    predating the claim-declared-monitor contract raises the leg's
    Inconclusive, which main renders as a stable digest line and a
    zero exit — and a doctored case over an inconclusive run still
    fails, since it can name no evidence."""

    def test_main_reports_the_inconclusive_digest(self):
        rc, out, err = run_main(
            outcome=leg.Inconclusive(
                "the pinned release predates the "
                "claim-declared-monitor contract"
            )
        )
        self.assertEqual(rc, 0)
        self.assertIn(
            "stranded-rejoin-digest inconclusive — the pinned "
            "release predates",
            out,
        )
        self.assertIn("stranded-rejoin: inconclusive", err)

    def test_two_inconclusive_passes_render_identically(self):
        first = run_main(outcome=leg.Inconclusive("pre-contract"))
        second = run_main(outcome=leg.Inconclusive("pre-contract"))
        self.assertEqual(first[0], 0)
        self.assertEqual(first[1], second[1])

    def test_a_doctored_case_over_an_inconclusive_run_fails(self):
        rc, out, err = run_main(
            tamper="expect-wedge",
            outcome=leg.Inconclusive("pre-contract"),
        )
        self.assertEqual(rc, 1)
        self.assertIn(
            "the doctored expectation wanted the demoted peer "
            "stranded",
            err,
        )


class Classification(unittest.TestCase):
    """main()'s exit classification over the pass result — failures
    stream to stderr prefixed by the stem, a doctored case may never
    exit zero, and a clean pass renders the sha digest line."""

    def test_failures_report_by_name(self):
        rc, out, err = run_main(
            outcome=([], {}, ["the demoted peer never re-joined"])
        )
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn(
            "stranded-rejoin: the demoted peer never re-joined", err
        )

    def test_an_abort_reports_by_name(self):
        rc, out, err = run_main(
            outcome=leg.Abort("the pair never converged")
        )
        self.assertEqual(rc, 1)
        self.assertIn("stranded-rejoin: the pair never converged", err)

    def test_a_doctored_case_never_passes_silently(self):
        rc, out, err = run_main(
            tamper="expect-wedge", outcome=([], {}, [])
        )
        self.assertEqual(rc, 1)
        self.assertIn("passed silently", err)

    def test_a_doctored_case_carrying_failures_still_fails(self):
        rc, _, _ = run_main(
            tamper="expect-wedge",
            outcome=([], {}, ["the honest run re-joined"]),
        )
        self.assertEqual(rc, 1)

    def test_a_clean_pass_renders_the_digest_line(self):
        evidence = {
            "converged": 4,
            "restored_at": 18,
        }
        rc, out, err = run_main(
            outcome=([{"phase": "converge"}], evidence, [])
        )
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"^stranded-rejoin-digest [0-9a-f]{64} — ")
        self.assertIn("tracking by tick 4", out)
        self.assertIn("restored at tick 18", out)


if __name__ == "__main__":
    unittest.main()
