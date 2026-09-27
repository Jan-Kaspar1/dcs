"""The resume_settle_once leg's unit coverage — ci/legs/
resume_settle_once.py is the reference plant's consumer-boundary
mirror of the qa rig's resume-settle-once scenario
(`2090_resume_settle_once.py`), proving the #1056
settle-once-across-resume contract on the released pair: a
receipted command suspended at the holder's fenced demote, the
promoted peer carrying and applying it plus a newer same-point
command, and the quiesced holder's restart-as-active never
re-applying the stale admission — one `command_settled` per
admission across both peers' journals, the rejoined incumbent's
served verdicts stable. These tests pin, without launching the
pair: the leg's registration record, its receipt/journal
projections, the contract-surface gate that classifies a
pre-contract release inconclusive, and the inconclusive /
doctored-case classifications the harness relies on — the new
leg's evidence lines and inconclusive handling asserted as the
issue requires."""
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
_LEG_PATH = _CI_DIR / "legs" / "resume_settle_once.py"
_MODEL_PATH = _ROOT / "reference-plant" / "model" / "plant.json"
_SCENARIO_PATH = _CI_DIR / "scenario.json"
_MANIFEST_PATH = _ROOT / "reference-plant" / "deploy" / "manifest.json"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


legs = load(_CI_DIR / "legs.py", "legs")
leg = load(_LEG_PATH, "resume_settle_once")

POINT = 1080
COMMAND = {
    "write_value": {"kind": "bool", "point": POINT, "value": {"bool": True}}
}
ACTOR = "ci-resume-susp"


def argv(tamper=None):
    args = [
        "resume_settle_once.py",
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
    or the exception resume_settle_pass raises. Returns
    `(rc, stdout, stderr)`."""
    if isinstance(outcome, BaseException):
        stub = mock.Mock(side_effect=outcome)
    else:
        stub = mock.Mock(return_value=outcome)
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", argv(tamper)), \
            mock.patch.object(leg, "resume_settle_pass", stub), \
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
        record = discovered["resume_settle_once.py"]
        self.assertEqual(record["stem"], "resume-settle-once")
        self.assertEqual(record["order"], 440)

    def test_the_named_diagnostics_follow_the_stem_convention(self):
        # The issue names resume-settle-once-failed and
        # resume-settle-once-nondeterministic — the driver's defaults
        # off the file stem, undeclared in the literal.
        self.assertNotIn("failed", leg.LEG)
        self.assertEqual(leg.LEG["passes"], "resume-settle-once")

    def test_the_doctored_case_carries_named_evidence(self):
        tampers = {entry["name"]: entry for entry in leg.LEG["tampers"]}
        self.assertEqual(set(tampers), {"expect-reapply"})
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
    admission's (command, actor) identity, the `command_settled`
    records projected to (seq, tick, outcome), the promotable sync
    vocabulary, and the persisted `--state-file` checkpoint read."""

    def test_admission_hit_matches_command_and_actor(self):
        self.assertTrue(
            leg.admission_hit(receipt(), COMMAND, ACTOR))
        self.assertFalse(
            leg.admission_hit(receipt(actor="other"), COMMAND, ACTOR))
        self.assertFalse(
            leg.admission_hit(receipt(command={}), COMMAND, ACTOR))
        self.assertFalse(leg.admission_hit(None, COMMAND, ACTOR))
        self.assertFalse(leg.admission_hit("applied", COMMAND, ACTOR))

    def test_admission_settles_projects_in_seq_order(self):
        entries = [
            settled_entry(3, 7, outcome={"applied": {}}),
            entry(4, 8, {"point_changed": {"point": POINT, "to": {}}}),
            settled_entry(5, 9, actor="other", outcome={"applied": {}}),
            settled_entry(6, 10, outcome={"applied": {}}),
        ]
        self.assertEqual(
            leg.admission_settles(entries, COMMAND, ACTOR),
            [(3, 7, "applied"), (6, 10, "applied")],
        )

    def test_admission_settles_ignores_other_events_and_admissions(self):
        entries = [
            entry(3, 7, {"command_settled": {"receipt": receipt(
                actor="other")}}),
            entry(4, 8, {"role_changed": {"from": "active"}}),
            entry(5, 8, {"command_admitted": {"receipt": receipt()}}),
        ]
        self.assertEqual(
            leg.admission_settles(entries, COMMAND, ACTOR), [])

    def test_tracking_reads_the_standby_sync_state(self):
        self.assertTrue(leg.tracking(
            {"role": "standby", "sync": {"tracking": {"aligned": 9}}}))
        self.assertFalse(leg.tracking(
            {"role": "standby", "sync": {"orphaned": {}}}))
        self.assertFalse(leg.tracking(
            {"role": "active", "sync": {"tracking": {"aligned": 9}}}))
        self.assertFalse(leg.tracking(
            {"role": "standby", "sync": "unsynchronized"}))

    def test_promotable_accepts_the_converged_sync_states(self):
        for sync in ({"tracking": {}}, {"orphaned": {}},
                     {"reinitialized": {}}):
            self.assertTrue(leg.promotable(
                {"role": "standby", "sync": sync}), sync)
        self.assertFalse(leg.promotable(
            {"role": "standby", "sync": "unsynchronized"}))
        self.assertFalse(leg.promotable(
            {"role": "active", "sync": {"tracking": {}}}))
        self.assertFalse(leg.promotable({"role": "standby"}))

    def test_state_checkpoint_reads_the_persisted_document(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "state.json"
            path.write_text(json.dumps(
                {"tick": 9, "source_owns_field": False,
                 "receipts": [receipt()]}))
            document = leg.state_checkpoint(str(path))
            self.assertEqual(document["tick"], 9)
            self.assertFalse(document["source_owns_field"])
            self.assertEqual(document["receipts"][0]["actor"], ACTOR)
            self.assertIsNone(
                leg.state_checkpoint(str(Path(scratch) / "absent")))
            path.write_text("{partial")
            self.assertIsNone(leg.state_checkpoint(str(path)))


class ContractSurface(unittest.TestCase):
    """The release-precedence gate: the served checkpoint must carry
    the receipt window, the admission counters, and the
    `source_owns_field` stamp the resume gate reads — any absence is
    the pinned release predating the contract, never a violation."""

    def surface(self, checkpoint):
        failures = []
        with mock.patch.object(
            leg.pair, "get", return_value=checkpoint
        ):
            return leg.checkpoint_surface(
                "http://peer", "GET /checkpoint", failures)

    def test_the_full_surface_passes(self):
        checkpoint = {
            "receipts": [receipt()],
            "command_admission": {"attempts": 3},
            "source_owns_field": True,
        }
        self.assertEqual(self.surface(checkpoint), checkpoint)

    def test_each_missing_field_is_inconclusive(self):
        for checkpoint in (
            {"command_admission": {"attempts": 3},
             "source_owns_field": True},
            {"receipts": [], "source_owns_field": True},
            {"receipts": [], "command_admission": {"attempts": 3}},
            {"receipts": [], "command_admission": {},
             "source_owns_field": True},
            {"receipts": [], "command_admission": {"attempts": 3}},
        ):
            with self.assertRaises(leg.Inconclusive, msg=checkpoint):
                self.surface(checkpoint)


class Inconclusive(unittest.TestCase):
    """The release-precedence classification: a pinned release
    predating the settle-once-across-resume contract raises the
    leg's Inconclusive, which main renders as a stable digest line
    and a zero exit — and a doctored case over an inconclusive run
    still fails, since it can name no evidence."""

    def test_main_reports_the_inconclusive_digest(self):
        rc, out, err = run_main(
            outcome=leg.Inconclusive(
                "the pinned release predates the "
                "resume-settle contract"
            )
        )
        self.assertEqual(rc, 0)
        self.assertIn(
            "resume-settle-once-digest inconclusive — the pinned "
            "release predates",
            out,
        )
        self.assertIn("resume-settle-once: inconclusive", err)

    def test_two_inconclusive_passes_render_identically(self):
        first = run_main(outcome=leg.Inconclusive("pre-contract"))
        second = run_main(outcome=leg.Inconclusive("pre-contract"))
        self.assertEqual(first[0], 0)
        self.assertEqual(first[1], second[1])

    def test_a_doctored_case_over_an_inconclusive_run_fails(self):
        rc, out, err = run_main(
            tamper="expect-reapply",
            outcome=leg.Inconclusive("pre-contract"),
        )
        self.assertEqual(rc, 1)
        self.assertIn(
            "the doctored expectation wanted the resumed peer "
            "re-applying",
            err,
        )


class Classification(unittest.TestCase):
    """main()'s exit classification over the pass result — failures
    stream to stderr prefixed by the stem, a doctored case may never
    exit zero, and a clean pass renders the sha digest line."""

    def test_failures_report_by_name(self):
        rc, out, err = run_main(
            outcome=([], {}, ["the restarted peer re-applied"])
        )
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn(
            "resume-settle-once: the restarted peer re-applied", err
        )

    def test_an_abort_reports_by_name(self):
        rc, out, err = run_main(
            outcome=leg.Abort("the pair never converged")
        )
        self.assertEqual(rc, 1)
        self.assertIn("resume-settle-once: the pair never converged", err)

    def test_a_doctored_case_never_passes_silently(self):
        rc, out, err = run_main(
            tamper="expect-reapply", outcome=([], {}, [])
        )
        self.assertEqual(rc, 1)
        self.assertIn("passed silently", err)

    def test_a_doctored_case_carrying_failures_still_fails(self):
        rc, _, _ = run_main(
            tamper="expect-reapply",
            outcome=([], {}, ["the honest run parked the receipt"]),
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
        self.assertRegex(
            out, r"^resume-settle-once-digest [0-9a-f]{64} — ")
        self.assertIn("tracking by tick 4", out)
        self.assertIn("restored at tick 18", out)


if __name__ == "__main__":
    unittest.main()
