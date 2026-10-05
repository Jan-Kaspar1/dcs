"""The gossip_repromote_settle leg's unit coverage — ci/legs/
gossip_repromote_settle.py is the reference plant's
consumer-boundary mirror of the qa rig's gossip-repromote-settle
scenario (`1965_gossip_repromote_settle.py`), proving the #708
contract's gossip-window variant on the released pair: a receipted
command the holder suspends `Accepted` at its own demote resolves
when that same holder is re-promoted before any peer's tracking pull
covers the admission — settled once at the re-taken boundary, never
parked `Accepted` on the live active and never resurrected stale
behind a newer command. These tests pin, without launching the pair:
the leg's registration record, the promotable-verdict gate the
gossip-window re-promote rides, and the inconclusive / doctored-case
classifications the harness relies on — including the doctored cases'
declared evidence surviving an inconclusive run so the stage's
unchecked self-check reads it.
"""
import contextlib
import importlib.util
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
_CI_DIR = _ROOT / "reference-plant" / "ci"
_LEG_PATH = _CI_DIR / "legs" / "gossip_repromote_settle.py"
_MODEL_PATH = _ROOT / "reference-plant" / "model" / "plant.json"
_SCENARIO_PATH = _CI_DIR / "scenario.json"
_MANIFEST_PATH = _ROOT / "reference-plant" / "deploy" / "manifest.json"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


legs = load(_CI_DIR / "legs.py", "legs")
leg = load(_LEG_PATH, "gossip_repromote_settle")


def argv(tamper=None):
    args = [
        "gossip_repromote_settle.py",
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
    or the exception gossip_pass raises. Returns
    `(rc, stdout, stderr)`."""
    if isinstance(outcome, BaseException):
        stub = mock.Mock(side_effect=outcome)
    else:
        stub = mock.Mock(return_value=outcome)
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", argv(tamper)), \
            mock.patch.object(leg, "gossip_pass", stub), \
            contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        rc = leg.main()
    return rc, out.getvalue(), err.getvalue()


class Registration(unittest.TestCase):
    """The leg's `LEG` literal — the pair stage's discovery contract:
    the declared order is unique across the directory, the stem and
    the named diagnostics follow the file-name convention, and the
    doctored cases declare the evidence the leg reports."""

    def test_the_leg_registers_in_the_pair_stage(self):
        discovered = {
            Path(record["file"]).name: record
            for record in legs.discover(str(_CI_DIR / "legs"))
        }
        record = discovered["gossip_repromote_settle.py"]
        self.assertEqual(record["stem"], "gossip-repromote-settle")
        self.assertEqual(record["order"], 495)

    def test_the_named_diagnostics_follow_the_stem_convention(self):
        # The issue names gossip-repromote-settle-failed and
        # gossip-repromote-settle-nondeterministic — the driver's
        # defaults off the file stem, so the literal declares no
        # `failed` override.
        self.assertNotIn("failed", leg.LEG)
        self.assertEqual(leg.LEG["passes"], "gossip-repromote-settle")

    def test_the_doctored_cases_carry_named_evidence(self):
        tampers = {entry["name"]: entry for entry in leg.LEG["tampers"]}
        self.assertEqual(set(tampers), {"expect-parked", "expect-stale"})
        # The declared evidence must be a line the leg actually
        # prints — pinned against the source so a drifted message
        # cannot pass the harness's substring check by accident.
        source = _LEG_PATH.read_text()
        for name, record in tampers.items():
            self.assertTrue(record["evidence"], name)
            for evidence in record["evidence"]:
                self.assertIn(evidence, source, name)
            for field in ("passed", "missed"):
                self.assertIsInstance(record[field], str)


class PromotableVerdict(unittest.TestCase):
    """The verdict gate the in-window re-promote rides: only a
    standby under a tracked-line sync variant is promotable — the
    ownerless line's `orphaned` reading the same promotable verdict
    `tracking` stands on."""

    def test_the_tracked_line_states_are_promotable(self):
        for sync in (
            {"tracking": {"aligned": 9}},
            {"orphaned": {}},
            {"reinitialized": {}},
            {"usurped": {}},
        ):
            self.assertTrue(
                leg.promotable({"role": "standby", "sync": sync}), sync)

    def test_everything_else_is_not_promotable(self):
        for report in (
            {"role": "standby", "sync": {"unsynchronized": {}}},
            {"role": "standby"},
            {"role": "standby", "sync": "tracking"},
            {"role": "active", "sync": {"tracking": {}}},
            {"role": "demoting"},
            {},
            None,
        ):
            self.assertFalse(leg.promotable(report), report)


class Inconclusive(unittest.TestCase):
    """The release-precedence classification: a pinned release
    predating the suspended-receipt contract raises the leg's
    Inconclusive, which main renders as a stable digest line and a
    zero exit — and a doctored case over an inconclusive run still
    fails carrying its declared evidence, so the stage's unchecked
    self-check reads the tamper's own line rather than reporting the
    leg's audit missing."""

    def test_main_reports_the_inconclusive_digest(self):
        rc, out, err = run_main(
            outcome=leg.Inconclusive(
                "the pinned release predates the "
                "gossip-repromote-settle contract"
            )
        )
        self.assertEqual(rc, 0)
        self.assertIn(
            "gossip-repromote-settle-digest inconclusive — the pinned "
            "release predates",
            out,
        )
        self.assertIn("gossip-repromote-settle: inconclusive", err)

    def test_two_inconclusive_passes_render_identically(self):
        first = run_main(outcome=leg.Inconclusive("pre-contract"))
        second = run_main(outcome=leg.Inconclusive("pre-contract"))
        self.assertEqual(first[0], 0)
        self.assertEqual(first[1], second[1])

    def test_a_doctored_case_over_an_inconclusive_run_fails_with_its_evidence(self):
        # The contract the stage's unchecked self-check relies on: the
        # tampered run must fail *carrying its declared evidence* — an
        # inconclusive run echoes the doctored expectation it could
        # not reach rather than reporting nothing.
        for tamper in ("expect-parked", "expect-stale"):
            rc, _out, err = run_main(
                tamper=tamper,
                outcome=leg.Inconclusive("pre-contract"),
            )
            self.assertEqual(rc, 1, tamper)
            declared = next(
                entry["evidence"][0]
                for entry in leg.LEG["tampers"]
                if entry["name"] == tamper
            )
            self.assertIn(declared, err, tamper)


class Classification(unittest.TestCase):
    """main()'s exit classification over the pass result — failures
    stream to stderr prefixed by the stem, a doctored case may never
    exit zero, and a clean pass renders the sha digest line."""

    def test_failures_report_by_name(self):
        rc, out, err = run_main(
            outcome=([], {}, ["the admission is still parked Accepted"])
        )
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn(
            "gossip-repromote-settle: the admission is still parked "
            "Accepted",
            err,
        )

    def test_an_abort_reports_by_name(self):
        rc, _out, err = run_main(
            outcome=leg.Abort("the pair never converged")
        )
        self.assertEqual(rc, 1)
        self.assertIn(
            "gossip-repromote-settle: the pair never converged", err)

    def test_a_doctored_case_never_passes_silently(self):
        rc, _out, err = run_main(
            tamper="expect-parked", outcome=([], {}, [])
        )
        self.assertEqual(rc, 1)
        self.assertIn("passed silently", err)

    def test_a_doctored_case_carrying_failures_still_fails(self):
        rc, _out, err = run_main(
            tamper="expect-stale",
            outcome=([], {}, ["the doctored expectation wanted the "
                             "stale value"]),
        )
        self.assertEqual(rc, 1)
        self.assertIn("the doctored expectation", err)

    def test_a_clean_pass_renders_the_digest_line(self):
        rc, out, err = run_main(
            outcome=(
                [{"phase": "converge"}, {"phase": "audit"}],
                {"converged": 9, "indices": {"duty": 4},
                 "restored_at": 30},
                [],
            )
        )
        self.assertEqual(rc, 0, err)
        self.assertIn("gossip-repromote-settle-digest ", out)
        self.assertIn("re-promoted inside the gossip window", out)
        self.assertEqual(err, "")


if __name__ == "__main__":
    unittest.main()
