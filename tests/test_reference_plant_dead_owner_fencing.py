"""The dead_owner_fencing leg's unit coverage — ci/legs/
dead_owner_fencing.py is the reference plant's consumer-boundary
mirror of the qa rig's dead-owner scenario (#672's
2487_dead_owner_fencing.py), proving #638's never-released rule on the
released pair: a claim a closed owner leaves standing keeps fencing
every third attachment's mutation across the server-side reap of its
hold, a release from an attachment holding nothing answers done and
changes nothing, the recorded owner's token re-arms and writes, and a
preempting claim takes the field per the declared contract. These
tests pin, without launching the pair: the leg's registration record,
the staging token separation from its neighbours', and the
inconclusive / doctored-case classifications the harness relies on."""
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
_LEG_PATH = _CI_DIR / "legs" / "dead_owner_fencing.py"
_SCENARIO_PATH = _CI_DIR / "scenario.json"
_MANIFEST_PATH = _ROOT / "reference-plant" / "deploy" / "manifest.json"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(_CI_DIR))
    sys.path.insert(0, str(_CI_DIR / "legs"))
    spec.loader.exec_module(module)
    return module


legs = load(_CI_DIR / "legs.py", "legs")
leg = load(_LEG_PATH, "dead_owner_fencing")


def argv(tamper=None):
    args = [
        "dead_owner_fencing.py",
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
    or the exception dead_owner_pass raises. Returns
    `(rc, stdout, stderr)`."""
    if isinstance(outcome, BaseException):
        stub = mock.Mock(side_effect=outcome)
    else:
        stub = mock.Mock(return_value=outcome)
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", argv(tamper)), \
            mock.patch.object(leg, "dead_owner_pass", stub), \
            contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        rc = leg.main()
    return rc, out.getvalue(), err.getvalue()


class Registration(unittest.TestCase):
    """The leg's `LEG` literal — the pair stage's discovery contract:
    the declared order is unique across the directory, the stem and
    the named diagnostics follow the file-name convention, and the
    doctored case declares the evidence the honest run reports."""

    def test_the_leg_registers_in_the_pair_stage(self):
        discovered = legs.discover(str(_CI_DIR / "legs"))
        orders = [record["order"] for record in discovered]
        self.assertEqual(len(orders), len(set(orders)),
                         "two legs declare the same order")
        record = {
            Path(entry["file"]).name: entry for entry in discovered
        }["dead_owner_fencing.py"]
        self.assertEqual(record["stem"], "dead-owner-fencing")
        self.assertEqual(record["order"], 382)
        # Filed between the claim-reclaim leg it sits beside in the
        # claim lifecycle and the claim-observed leg behind it.
        by_stem = {entry["stem"]: entry["order"]
                   for entry in discovered}
        self.assertLess(by_stem["claim-reclaim"], record["order"])
        self.assertLess(record["order"], by_stem["claim-observed"])

    def test_the_named_diagnostics_follow_the_stem_convention(self):
        # The issue names dead-owner-fencing-failed and
        # dead-owner-fencing-nondeterministic — the driver's defaults
        # off the file stem, undeclared in the literal.
        self.assertNotIn("failed", leg.LEG)
        self.assertEqual(leg.LEG["passes"], "dead-owner-fencing")

    def test_the_doctored_case_carries_named_evidence(self):
        tampers = {entry["name"]: entry for entry in leg.LEG["tampers"]}
        self.assertEqual(set(tampers), {"expect-dissolved"})
        source = _LEG_PATH.read_text()
        for name, record in tampers.items():
            self.assertTrue(record["evidence"], name)
            for evidence in record["evidence"]:
                self.assertIn(evidence, source, name)
            for field in ("passed", "missed"):
                self.assertIsInstance(record[field], str)


class Staging(unittest.TestCase):
    """The staging tokens and window shape — the leg's own constants,
    separated from every neighbouring leg's induction token so the two
    legs cannot seize each other's claims, and the reap window long
    enough to span the server-side hold reap it exists to observe."""

    def test_the_staging_tokens_are_separated(self):
        claim_reclaim = load(
            _CI_DIR / "legs" / "claim_reclaim.py", "claim_reclaim_neigh"
        )
        for token in (leg.DEAD_OWNER, leg.PREEMPT_OWNER,
                      leg.FOREIGN_OWNER):
            self.assertNotEqual(token, claim_reclaim.FOREIGN_OWNER)
        self.assertEqual(
            len({leg.DEAD_OWNER, leg.PREEMPT_OWNER,
                 leg.FOREIGN_OWNER}),
            3,
            "the leg's three staging tokens must not collide",
        )

    def test_the_window_spans_the_reap(self):
        # The empty holder set is unobservable, so the window is
        # opened by repeating the non-holder sequence; a single round
        # could pass or fail on which side of the server's reap it
        # fell, which is exactly the nondeterminism the repetition
        # removes.
        self.assertGreater(leg.WINDOW_ROUNDS, 1)


class Inconclusive(unittest.TestCase):
    """The release-precedence classification: a pinned release
    predating the never-released claim contract raises the leg's
    Inconclusive, which main renders as a stable digest line and a
    zero exit — and a doctored case over an inconclusive run still
    fails, since it can name no evidence."""

    def test_main_reports_the_inconclusive_digest(self):
        rc, out, err = run_main(
            outcome=leg.Inconclusive(
                "the pinned release predates the never-released claim "
                "contract"
            )
        )
        self.assertEqual(rc, 0)
        self.assertIn(
            "dead-owner-fencing-digest inconclusive — the pinned "
            "release predates",
            out,
        )
        self.assertIn("dead-owner-fencing: inconclusive", err)

    def test_two_inconclusive_passes_render_identically(self):
        first = run_main(outcome=leg.Inconclusive("pre-contract"))
        second = run_main(outcome=leg.Inconclusive("pre-contract"))
        self.assertEqual(first[0], 0)
        self.assertEqual(first[1], second[1])

    def test_a_doctored_case_over_an_inconclusive_run_fails(self):
        rc, out, err = run_main(
            tamper="expect-dissolved",
            outcome=leg.Inconclusive("pre-contract"),
        )
        self.assertEqual(rc, 1)
        self.assertIn(
            "the doctored expectation wanted the dead owner's claim "
            "dissolved",
            err,
        )


class Classification(unittest.TestCase):
    """main()'s exit classification over the pass result — failures
    stream to stderr prefixed by the stem, a doctored case may never
    exit zero, and a clean pass renders the sha digest line."""

    def test_failures_report_by_name(self):
        rc, out, err = run_main(
            outcome=([], {}, ["a non-holder release dissolved the "
                              "dead owner's claim"])
        )
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("dead-owner-fencing: a non-holder release", err)

    def test_an_abort_reports_by_name(self):
        rc, out, err = run_main(
            outcome=leg.Abort("the pair never converged")
        )
        self.assertEqual(rc, 1)
        self.assertIn(
            "dead-owner-fencing: the pair never converged", err
        )

    def test_a_doctored_case_never_passes_silently(self):
        rc, out, err = run_main(
            tamper="expect-dissolved", outcome=([], {}, [])
        )
        self.assertEqual(rc, 1)
        self.assertIn("passed silently", err)

    def test_a_doctored_case_carrying_failures_still_fails(self):
        rc, _, _ = run_main(
            tamper="expect-dissolved",
            outcome=([], {}, ["named evidence"]),
        )
        self.assertEqual(rc, 1)

    def test_a_clean_pass_renders_the_digest_line(self):
        evidence = {
            "window": [{"round": index} for index
                       in range(leg.WINDOW_ROUNDS)],
            "restored": {"result": "error"},
        }
        rc, out, err = run_main(
            outcome=([{"phase": "converge"}], evidence, [])
        )
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"^dead-owner-fencing-digest [0-9a-f]{64} — ")
        self.assertIn(
            f"{leg.WINDOW_ROUNDS} reap-window rounds", out)


if __name__ == "__main__":
    unittest.main()