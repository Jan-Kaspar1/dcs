"""The unclaimed_rearm leg's unit coverage — ci/legs/
unclaimed_rearm.py is the reference plant's consumer-boundary mirror
of the qa rig's unclaimed-field re-arm scenario (scenario 3700,
pinned on the settled #621 behavior the qax-20260918-008 run
verified): a field write refused `unclaimed` while no claim stands
is a recoverable ownerless window, not supersession — the recorded
owner re-arms inline through one conditional, non-preempting
`ensure_writer`, its write lands without `field_claim_lost` or
demotion, while a write refused under another standing claim still
fences and demotes. These tests pin, without launching the pair: the
leg's registration record, the verdict classifications its window,
re-arm and foreign probes are read through, the staging seams it
reuses rather than reimplements, and the inconclusive / doctored-case
classifications `main` renders."""
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
_LEG_PATH = _CI_DIR / "legs" / "unclaimed_rearm.py"
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
leg = load(_LEG_PATH, "unclaimed_rearm")
model = json.loads(_MODEL_PATH.read_text())


def argv(tamper=None):
    args = [
        "unclaimed_rearm.py",
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
    or the exception unclaimed_rearm_pass raises. Returns
    `(rc, stdout, stderr)`."""
    if isinstance(outcome, BaseException):
        stub = mock.Mock(side_effect=outcome)
    else:
        stub = mock.Mock(return_value=outcome)
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", argv(tamper)), \
            mock.patch.object(leg, "unclaimed_rearm_pass", stub), \
            contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        rc = leg.main()
    return rc, out.getvalue(), err.getvalue()


def entry(seq, tick, event):
    return {"seq": seq, "tick": tick, "event": event}


class Registration(unittest.TestCase):
    """The leg's `LEG` literal — the pair stage's discovery contract:
    the declared order is free and unique in the directory, the stem
    and the named diagnostics follow the file-name convention, and the
    doctored case declares the evidence the honest run reports."""

    def test_the_leg_registers_in_the_pair_stage(self):
        discovered = legs.discover(str(_CI_DIR / "legs"))
        record = [
            leg for leg in discovered if leg["stem"] == "unclaimed-rearm"
        ]
        self.assertEqual(len(record), 1)
        # The slot the leg declared, the one after the
        # usurped-claim-reclaim leg that reached main first.
        self.assertEqual(record[0]["order"], 810)
        orders = [leg["order"] for leg in discovered]
        self.assertEqual(len(orders), len(set(orders)))

    def test_the_named_diagnostics_follow_the_stem_convention(self):
        # The issue names unclaimed-rearm-failed and
        # unclaimed-rearm-nondeterministic — the driver's defaults off
        # the file stem, undeclared in the literal.
        self.assertNotIn("failed", leg.LEG)
        self.assertEqual(leg.LEG["passes"], "unclaimed-rearm")

    def test_the_doctored_case_carries_named_evidence(self):
        tampers = {case["name"]: case for case in leg.LEG["tampers"]}
        self.assertEqual(set(tampers), {"expect-foreign"})
        source = _LEG_PATH.read_text()
        for name, case in tampers.items():
            self.assertTrue(case["evidence"], name)
            for evidence in case["evidence"]:
                self.assertIn(evidence, source, name)
            for field in ("passed", "missed"):
                self.assertIsInstance(case[field], str)

    def test_the_contract_declares_the_emitted_diagnostics(self):
        contract = (_ROOT / "docs" / "release-contract.md").read_text()
        for name in (
            "`unclaimed-rearm-failed`",
            "`unclaimed-rearm-nondeterministic`",
            "`unclaimed-rearm-unchecked`",
        ):
            self.assertIn(name, contract)


class Classifications(unittest.TestCase):
    """The verdict word functions the episode reads its window,
    re-arm and foreign probes through — the recoverable ownerless
    window, the fail-closed shapes, the point-level write refusal,
    and the claim-identity word the digest carries in place of a
    minted token."""

    def test_an_ownerless_window_reads_unclaimed_and_fails_closed(self):
        unclaimed = {
            "result": "error",
            "error": {
                "kind": "unclaimed",
                "detail": "no attachment holds field writes",
            },
        }
        self.assertEqual(leg.failover.probe_kind(unclaimed), "unclaimed")
        self.assertTrue(leg.fail_closed("unclaimed"))
        self.assertFalse(leg.claim_reclaim.mutation_fenced(unclaimed))

    def test_a_standing_claim_reads_fenced_and_fails_closed(self):
        fenced = {
            "result": "error",
            "error": {
                "kind": "fenced",
                "detail": "another attachment owns field writes",
                "owner": 4242,
            },
        }
        self.assertEqual(leg.failover.probe_kind(fenced), "fenced")
        self.assertTrue(leg.fail_closed("fenced"))
        self.assertEqual(leg.claim_reclaim.verdict_owner(fenced), 4242)

    def test_only_a_granted_mutation_is_the_defect_shape(self):
        granted = {"result": "done"}
        self.assertEqual(leg.failover.probe_kind(granted), "granted")
        self.assertFalse(leg.fail_closed("granted"))

    def test_a_foreign_write_probe_reads_the_point_level_refusal(self):
        fenced = {
            "result": "error",
            "error": {
                "kind": "io",
                "error": {"fenced": 10},
                "owner": 4242,
            },
        }
        self.assertTrue(leg.write_fenced(fenced))
        self.assertTrue(leg.claim_reclaim.mutation_fenced(fenced))
        self.assertFalse(leg.write_fenced({"result": "done"}))
        # The plant-level verdicts carry no point-level counterpart.
        self.assertFalse(
            leg.write_fenced(
                {
                    "result": "error",
                    "error": {"kind": "fenced", "owner": 4242},
                }
            )
        )

    def test_the_claim_identity_word_never_carries_a_minted_token(self):
        self.assertEqual(leg.owner_word(7, 7), "owner")
        self.assertEqual(leg.owner_word(leg.FOREIGN_OWNER, 7), "foreign")
        self.assertEqual(leg.owner_word(None, 7), "unowned")
        self.assertEqual(leg.owner_word(9, 7), "other")

    def test_the_fencing_loss_ledger_reads_io_health(self):
        self.assertEqual(
            leg.ledger({"io_health": {"failed_writes": 3}}), 3
        )
        self.assertIsNone(leg.ledger({"io_health": {}}))
        # A snapshot predating the section is the pre-contract shape,
        # never a run that lost a write.
        self.assertIsNone(leg.ledger({"tick": 4}))
        self.assertIsNone(leg.ledger(None))

    def test_the_probe_point_is_a_channel_backed_field_input(self):
        # Internal points carry no channel and no plant-side binding,
        # so the foreign write probe needs a channel point — the
        # lowest-id field input the emitted model declares.
        probe = leg.field_probe_point(model)
        declared = {
            point["id"]: point for point in model["io_points"]
        }
        self.assertIn(probe, declared)
        self.assertTrue(declared[probe].get("channel"))
        self.assertEqual(declared[probe]["direction"], "in")
        self.assertIsNone(
            leg.field_probe_point({"io_points": [{"id": 1, "writable": True}]})
        )

    def test_the_durable_journal_projection_reads_entry_records(self):
        # The durable half of the window's audit reads the declared
        # journal file's entries in file order — the run's cold-start
        # boundary marker is not an entry.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.jsonl"
            path.write_text(
                json.dumps({"run_boundary": {"run": 1, "tick": 0}})
                + "\n"
                + json.dumps({"entry": entry(1, 1, {"point_changed": {
                    "point": 100, "to": True}})})
                + "\n"
            )
            self.assertEqual(
                [record["seq"] for record in leg.journal_entries(path)],
                [1],
            )


class Staging(unittest.TestCase):
    """The seams the leg drives rather than reimplements — the
    claim-reclaim staging's verdict and attribution reads, the
    failover field reads and claim-state words, and the tracking and
    converged-sync checks the watch and the settle assert on."""

    def test_the_leg_reuses_the_claim_reclaim_staging(self):
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
        self.assertFalse(claim_reclaim.unsupported_verb(verdict))
        self.assertTrue(
            claim_reclaim.tracking(
                {"role": "standby", "sync": {"tracking": {}}}
            )
        )
        self.assertTrue(
            claim_reclaim.converged_sync(
                {"role": "standby", "sync": {"orphaned": {}}}
            )
        )
        for helper in (
            claim_reclaim.mutation_fenced,
            claim_reclaim.verdict_owner,
            claim_reclaim.unsupported_verb,
            claim_reclaim.tracking,
            claim_reclaim.converged_sync,
        ):
            self.assertEqual(helper.__module__, "claim_reclaim")

    def test_the_induction_token_is_the_legs_own(self):
        # A fresh plant per leg means the fixed token cannot collide
        # with a controller's minted one — nor with the tokens the
        # other claim legs stage.
        for other in (
            leg.claim_reclaim.FOREIGN_OWNER,
            leg.stranded_rejoin.TOOL_CLAIM,
        ):
            self.assertNotEqual(leg.FOREIGN_OWNER, other)

    def test_the_leg_reads_the_journal_audit_through_stranded_rejoin(self):
        entries = [
            entry(3, 7, {"field_claim_lost": {"point": 5, "claimant": 9}}),
            entry(4, 8, {"role_changed": {
                "from": "active", "to": "demoting", "origin": "fenced"}}),
        ]
        self.assertEqual(
            leg.stranded_rejoin.lost_entries(entries),
            [(3, {"point": 5, "claimant": 9})],
        )
        self.assertEqual(
            leg.stranded_rejoin.role_walk(entries),
            [("active", "demoting", "fenced")],
        )

    def test_the_driven_scan_bounds_are_positive(self):
        for bound in (
            leg.REARM_SCANS,
            leg.DEMOTE_SCANS,
            leg.RECLAIM_SCANS,
            leg.SETTLE_TICKS,
        ):
            self.assertGreater(bound, 0)


class Inconclusive(unittest.TestCase):
    """The release-precedence classification: a pinned release
    predating the unclaimed-rearm contract raises the leg's
    Inconclusive, which main renders as a stable digest line and a
    zero exit — and a doctored case over an inconclusive run still
    fails, since it can name no evidence."""

    def test_main_reports_the_inconclusive_digest(self):
        rc, out, err = run_main(
            outcome=leg.Inconclusive(
                "a mutation with no claim standing is refused as a "
                "plain fencing verdict naming no owner",
                "the window probe was fenced naming no owner",
            )
        )
        self.assertEqual(rc, 0)
        self.assertIn(
            "unclaimed-rearm-digest inconclusive — a mutation with no "
            "claim standing is refused",
            out,
        )
        self.assertIn("unclaimed-rearm: inconclusive —", err)
        # The run's own verdict reports on stderr only, where two
        # identical passes need not share it.
        self.assertNotIn("fenced naming no owner", out)

    def test_two_inconclusive_passes_render_identically(self):
        first = run_main(outcome=leg.Inconclusive("pre-contract"))
        second = run_main(outcome=leg.Inconclusive("pre-contract"))
        self.assertEqual(first[0], 0)
        self.assertEqual(first[1], second[1])

    def test_a_doctored_case_over_an_inconclusive_run_fails(self):
        rc, out, err = run_main(
            tamper="expect-foreign",
            outcome=leg.Inconclusive("pre-contract"),
        )
        self.assertEqual(rc, 1)
        self.assertIn(
            "the doctored expectation wanted the claim to rest under "
            "the foreign claimant",
            err,
        )


class Classification(unittest.TestCase):
    """main()'s exit classification over the pass result — failures
    stream to stderr prefixed by the stem, a doctored case may never
    exit zero, and a clean pass renders the sha digest line."""

    def test_failures_report_by_name(self):
        rc, out, err = run_main(
            outcome=([], {}, ["the owner's write never landed"])
        )
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn(
            "unclaimed-rearm: the owner's write never landed", err
        )

    def test_an_abort_reports_by_name(self):
        rc, out, err = run_main(
            outcome=leg.Abort("the pair never converged")
        )
        self.assertEqual(rc, 1)
        self.assertIn("unclaimed-rearm: the pair never converged", err)

    def test_a_doctored_case_never_passes_silently(self):
        rc, out, err = run_main(tamper="expect-foreign", outcome=([], {}, []))
        self.assertEqual(rc, 1)
        self.assertIn("passed silently", err)

    def test_a_doctored_case_carrying_failures_still_fails(self):
        rc, _, err = run_main(
            tamper="expect-foreign", outcome=([], {}, ["named evidence"])
        )
        self.assertEqual(rc, 1)
        self.assertIn("named evidence", err)

    def test_a_clean_pass_renders_the_digest_line(self):
        evidence = {"converged": 4, "final_tick": 20}
        rc, out, err = run_main(
            outcome=([{"phase": "converge"}], evidence, [])
        )
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"^unclaimed-rearm-digest [0-9a-f]{64} — ")
        self.assertIn("tracking by tick 4", out)
        self.assertIn("reconverged to its launch roles at tick 20", out)
        self.assertEqual(err, "")


if __name__ == "__main__":
    unittest.main()