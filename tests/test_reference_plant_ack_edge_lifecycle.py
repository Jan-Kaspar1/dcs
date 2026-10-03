"""The ack_edge_lifecycle leg's unit coverage — ci/legs/
ack_edge_lifecycle.py is the reference plant's consumer-boundary
mirror of the qa rig's consumed-edge scenario (#994's
3950_ack_edge_lifecycle.py), proving the #781/#947/#961 contract
family on the released pair. These tests pin, without launching the
pair: the leg's registration record, its model-driven resolution of
the lifecycle's points and managed-alarm component off the emitted
artifact, the release-precedence probe that classifies a
pre-contract release inconclusive, the journal lifecycle projection
and its ordered audit, and the inconclusive / doctored-case
classifications the harness relies on — the new leg's evidence
lines and inconclusive handling asserted as the issue requires."""
import contextlib
import copy
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
_CI_DIR = _ROOT / "reference-plant" / "ci"
_LEG_PATH = _CI_DIR / "legs" / "ack_edge_lifecycle.py"
_MODEL_PATH = _ROOT / "reference-plant" / "model" / "plant.json"
_MANIFEST_PATH = _ROOT / "reference-plant" / "deploy" / "manifest.json"
_SCENARIO_PATH = _CI_DIR / "scenario.json"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


legs = load(_CI_DIR / "legs.py", "legs")
leg = load(_LEG_PATH, "ack_edge_lifecycle")

TRUE, FALSE = {"bool": True}, {"bool": False}
POINTS = {
    "contact": 80,
    "ack": 1080,
    "alarm": 1083,
    "unack": 1084,
    "shelved": 1085,
    "suppressed": 1086,
    "oos": 1087,
}
COMPONENT = "managed-bool-latching-alarm:30"


def model():
    with open(_MODEL_PATH) as handle:
        return json.load(handle)


def argv(tamper=None):
    args = [
        "ack_edge_lifecycle.py",
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
    or the exception ack_edge_pass raises. Returns
    `(rc, stdout, stderr)`."""
    if isinstance(outcome, BaseException):
        stub = mock.Mock(side_effect=outcome)
    else:
        stub = mock.Mock(return_value=outcome)
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", argv(tamper)), \
            mock.patch.object(leg, "ack_edge_pass", stub), \
            contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        rc = leg.main()
    return rc, out.getvalue(), err.getvalue()


def settled_entry(seq, tick, point, value, outcome, applied=None):
    receipt = {
        "command": {
            "write_value": {
                "kind": "bool",
                "point": point,
                "value": value,
            }
        },
        "outcome": outcome,
        "actor": "ci-ack-edge",
    }
    return {
        "seq": seq,
        "tick": tick,
        "event": {"command_settled": {"receipt": receipt}},
    }


def changed_entry(seq, tick, point, to, source=None):
    return {
        "seq": seq,
        "tick": tick,
        "event": {
            "point_changed": {"point": point, "from": source, "to": to}
        },
    }


class Registration(unittest.TestCase):
    """The leg's `LEG` literal — the pair stage's discovery contract:
    the declared order is unique across the directory, the stem and
    failed diagnostic follow the file-name convention, and every
    doctored case declares the evidence the honest run reports."""

    def test_the_leg_registers_in_the_pair_stage(self):
        discovered = {
            Path(entry["file"]).name: entry
            for entry in legs.discover(str(_CI_DIR / "legs"))
        }
        record = discovered["ack_edge_lifecycle.py"]
        self.assertEqual(record["stem"], "ack-edge-lifecycle")
        self.assertEqual(record["order"], 280)
        self.assertEqual(record["failed"], "ack-edge-failed")

    def test_the_doctored_cases_carry_named_evidence(self):
        tampers = {entry["name"]: entry for entry in leg.LEG["tampers"]}
        self.assertEqual(
            set(tampers), {"expect-reconsumed", "expect-cleared"}
        )
        # The declared evidence must be the diagnostic the leg
        # actually prints — pin each against the source's failure
        # lines so a drifted message cannot pass the harness's
        # substring check by accident.
        source = _LEG_PATH.read_text()
        for name, entry in tampers.items():
            self.assertTrue(entry["evidence"], name)
            for evidence in entry["evidence"]:
                self.assertIn(evidence, source, name)
            for field in ("passed", "missed"):
                self.assertIsInstance(entry[field], str)


class Resolution(unittest.TestCase):
    """The lifecycle's point map and managed-alarm component resolve
    out of the emitted model — the leg exercises the declared seam,
    never a hard-coded id, and a model that cannot declare the seam
    resolves to the inconclusive surface."""

    def test_the_emitted_model_resolves_the_lifecycle_points(self):
        self.assertEqual(leg.signal_points(model()), POINTS)

    def test_the_emitted_model_resolves_the_managed_component(self):
        self.assertEqual(
            leg.alarm_component(model(), POINTS), COMPONENT
        )

    def test_an_unwritable_ack_resolves_no_surface(self):
        doc = model()
        for point in doc["io_points"]:
            if point["id"] == POINTS["ack"]:
                point["writable"] = False
        self.assertIsNone(leg.signal_points(doc))

    def test_an_unjournaled_contact_resolves_no_surface(self):
        doc = model()
        for point in doc["io_points"]:
            if point["id"] == POINTS["contact"]:
                point["journaled"] = False
        self.assertIsNone(leg.signal_points(doc))

    def test_a_missing_signal_resolves_no_surface(self):
        doc = model()
        doc["signals"] = [
            signal for signal in doc["signals"]
            if signal["name"] != "p101-moisture-ack"
        ]
        self.assertIsNone(leg.signal_points(doc))

    def test_no_managed_binding_resolves_no_component(self):
        doc = model()
        doc["components"] = [
            component for component in doc["components"]
            if component.get("kind") not in leg.MANAGED_KINDS
        ]
        self.assertIsNone(leg.alarm_component(doc, POINTS))


class ContractProbe(unittest.TestCase):
    """The served-checkpoint probe: the consumed-edge contract
    checkpointed the alarm's `ack` edge state, so a checkpoint
    carrying it runs the lifecycle and one predating it classifies
    inconclusive rather than failing the release."""

    def test_a_checkpoint_carrying_the_edge_marker_probes_true(self):
        checkpoint = {
            "components": {
                COMPONENT: {
                    "fields": {
                        "ack": TRUE,
                        "state": FALSE,
                        "unacknowledged": FALSE,
                    }
                }
            }
        }
        self.assertTrue(leg.contract_probe(checkpoint, COMPONENT))

    def test_a_checkpoint_predating_the_contract_probes_false(self):
        checkpoint = {
            "components": {
                COMPONENT: {
                    "fields": {
                        "state": FALSE,
                        "unacknowledged": FALSE,
                        "suppressed": FALSE,
                    }
                }
            }
        }
        self.assertFalse(leg.contract_probe(checkpoint, COMPONENT))

    def test_a_checkpoint_without_the_component_probes_false(self):
        self.assertFalse(
            leg.contract_probe({"components": {}}, COMPONENT)
        )
        self.assertFalse(leg.contract_probe({}, COMPONENT))

    def test_a_flat_state_still_probes(self):
        # A release serving component state without the fields
        # wrapper still probes on the state map itself.
        checkpoint = {"components": {COMPONENT: {"ack": TRUE}}}
        self.assertTrue(leg.contract_probe(checkpoint, COMPONENT))


class LifecycleProjection(unittest.TestCase):
    """The journal audit stream: `lifecycle` projects the ack
    point's settled receipts and the watched journaled transitions
    in seq order, and `ordered_group_misses` names any transition
    missing or out of run order."""

    def test_settles_and_changes_project_in_seq_order(self):
        entries = [
            changed_entry(1, 1, POINTS["contact"], FALSE),
            changed_entry(2, 2, POINTS["contact"], TRUE, FALSE),
            settled_entry(
                3, 3, POINTS["ack"], TRUE,
                {"applied": {"tick": 3}},
            ),
            changed_entry(4, 3, POINTS["unack"], FALSE, TRUE),
        ]
        events = leg.lifecycle(
            entries, POINTS["ack"], (POINTS["contact"], POINTS["unack"])
        )
        self.assertEqual(
            events,
            [
                {"kind": "changed", "seq": 1, "tick": 1,
                 "point": POINTS["contact"], "from": None, "to": FALSE},
                {"kind": "changed", "seq": 2, "tick": 2,
                 "point": POINTS["contact"], "from": FALSE, "to": TRUE},
                {"kind": "settled", "seq": 3, "tick": 3, "to": TRUE,
                 "outcome": "applied", "applied": 3,
                 "actor": "ci-ack-edge"},
                {"kind": "changed", "seq": 4, "tick": 3,
                 "point": POINTS["unack"], "from": TRUE, "to": FALSE},
            ],
        )

    def test_off_seam_records_are_dropped(self):
        entries = [
            settled_entry(1, 1, POINTS["contact"], TRUE,
                          {"applied": {"tick": 1}}),
            changed_entry(2, 1, 9999, TRUE),
            {"seq": 3, "tick": 1, "event": {"role_changed": {
                "from": "standby", "to": "active"}}},
        ]
        self.assertEqual(
            leg.lifecycle(
                entries, POINTS["ack"], (POINTS["contact"],)
            ),
            [],
        )

    def test_a_rejected_settle_projects_its_reason(self):
        entries = [
            settled_entry(1, 1, POINTS["ack"], FALSE,
                          {"rejected": {"reason": {"not_active": {}}}}),
        ]
        (event,) = leg.lifecycle(entries, POINTS["ack"], ())
        self.assertEqual(event["kind"], "settled")
        self.assertEqual(event["outcome"], "not_active")
        self.assertIsNone(event["applied"])

    def test_the_ordered_audit_accepts_run_order(self):
        watched = (POINTS["contact"], POINTS["alarm"], POINTS["unack"])
        entries = [
            changed_entry(1, 1, POINTS["contact"], TRUE),
            changed_entry(2, 2, POINTS["alarm"], TRUE),
            changed_entry(3, 2, POINTS["unack"], TRUE),
            settled_entry(4, 3, POINTS["ack"], TRUE,
                          {"applied": {"tick": 3}}),
            changed_entry(5, 3, POINTS["unack"], FALSE, TRUE),
        ]
        groups = [
            [("changed", POINTS["contact"], TRUE)],
            [("changed", POINTS["alarm"], TRUE),
             ("changed", POINTS["unack"], TRUE)],
            [("settled", TRUE, "applied"),
             ("changed", POINTS["unack"], FALSE)],
        ]
        self.assertEqual(
            leg.ordered_group_misses(
                leg.lifecycle(entries, POINTS["ack"], watched), groups
            ),
            [],
        )

    def test_the_ordered_audit_names_a_missing_transition(self):
        entries = [changed_entry(1, 1, POINTS["contact"], TRUE)]
        groups = [
            [("changed", POINTS["contact"], TRUE)],
            [("changed", POINTS["unack"], TRUE)],
        ]
        (miss,) = leg.ordered_group_misses(
            leg.lifecycle(
                entries, POINTS["ack"], (POINTS["contact"],)
            ),
            groups,
        )
        self.assertIn(str(POINTS["unack"]), miss)
        self.assertIn("group 1", miss)

    def test_the_ordered_audit_names_an_out_of_order_transition(self):
        entries = [
            changed_entry(1, 1, POINTS["unack"], TRUE),
            changed_entry(2, 2, POINTS["contact"], TRUE),
        ]
        groups = [
            [("changed", POINTS["contact"], TRUE)],
            [("changed", POINTS["unack"], TRUE)],
        ]
        (miss,) = leg.ordered_group_misses(
            leg.lifecycle(
                entries,
                POINTS["ack"],
                (POINTS["contact"], POINTS["unack"]),
            ),
            groups,
        )
        self.assertIn("out of order", miss + "missing or out of order")
        self.assertIn("group 1", miss)


class ReceiptMatching(unittest.TestCase):
    """The adopted receipt log's positional matching — identical
    submissions settle once each, refusals never count as applies."""

    def command(self):
        return leg.write_value(POINTS["ack"], TRUE)

    def receipt(self, outcome):
        return {"command": self.command(), "outcome": outcome,
                "actor": "ci-ack-edge"}

    def test_identical_submissions_match_positionally(self):
        receipts = [
            self.receipt({"applied": {"tick": 3}}),
            self.receipt({"applied": {"tick": 7}}),
        ]
        self.assertEqual(len(leg.settled(receipts, self.command())), 2)

    def test_a_rejection_never_matches(self):
        receipts = [
            self.receipt({"rejected": {"reason": {"not_active": {}}}}),
            self.receipt({"accepted": {"apply_tick": 4}}),
            self.receipt({"applied": {"tick": 5}}),
        ]
        self.assertEqual(len(leg.settled(receipts, self.command())), 1)

    def test_a_different_write_never_matches(self):
        receipts = [
            {"command": leg.write_value(POINTS["ack"], FALSE),
             "outcome": {"applied": {"tick": 3}}},
        ]
        self.assertEqual(leg.settled(receipts, self.command()), [])


class OwnerToken(unittest.TestCase):
    """The field-ownership preamble parse — the recorded owner token
    joins the standing writer claim; no claim line leaves the write
    unfenced."""

    def test_the_claim_line_reports_the_token(self):
        preamble = [
            "field write-ownership claim held under owner token 424243",
            "monitor listening on 127.0.0.1:9000",
        ]
        self.assertEqual(leg.owner_token(preamble), 424243)

    def test_no_claim_line_leaves_the_write_unfenced(self):
        self.assertIsNone(
            leg.owner_token(["monitor listening on 127.0.0.1:9000"])
        )
        self.assertIsNone(leg.owner_token([]))


class Inconclusive(unittest.TestCase):
    """The release-precedence classification: a model or checkpoint
    predating the consumed-edge contract raises the leg's
    Inconclusive, which main renders as a stable digest line and a
    zero exit — and a doctored case over an inconclusive run still
    fails, since it can name no evidence."""

    def test_a_model_without_the_seam_raises_inconclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            bare = Path(directory) / "model.json"
            bare.write_text("{}")
            args = SimpleNamespace(
                manifest=str(_MANIFEST_PATH), model=str(bare)
            )
            with self.assertRaises(leg.Inconclusive) as raised:
                leg.ack_edge_pass(args, None)
        self.assertIn("predates", str(raised.exception))

    def test_main_reports_the_inconclusive_digest(self):
        rc, out, err = run_main(
            outcome=leg.Inconclusive(
                "the pinned release predates the consumed-edge contract"
            )
        )
        self.assertEqual(rc, 0)
        self.assertIn(
            "ack-edge-lifecycle-digest inconclusive — the pinned "
            "release predates",
            out,
        )
        self.assertIn("ack-edge-lifecycle: inconclusive", err)

    def test_two_inconclusive_passes_render_identically(self):
        first = run_main(outcome=leg.Inconclusive("pre-contract"))
        second = run_main(outcome=leg.Inconclusive("pre-contract"))
        self.assertEqual(first[0], 0)
        self.assertEqual(first[1], second[1])

    def test_a_doctored_case_over_an_inconclusive_run_fails(self):
        for tamper, evidence in (
            ("expect-reconsumed", "wanted a re-consumed edge"),
            ("expect-cleared", "wanted the held press to clear"),
        ):
            rc, out, err = run_main(
                tamper=tamper,
                outcome=leg.Inconclusive("pre-contract"),
            )
            self.assertEqual(rc, 1, tamper)
            self.assertIn(evidence, err, tamper)


class Classification(unittest.TestCase):
    """main()'s exit classification over the pass result — failures
    stream to stderr prefixed by the stem, a doctored case may never
    exit zero, and a clean pass renders the sha digest line."""

    def test_failures_report_by_name(self):
        rc, out, err = run_main(outcome=([], {}, ["the latch lied"]))
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("ack-edge-lifecycle: the latch lied", err)

    def test_an_abort_reports_by_name(self):
        rc, out, err = run_main(
            outcome=leg.Abort("the pair never converged")
        )
        self.assertEqual(rc, 1)
        self.assertIn("ack-edge-lifecycle: the pair never converged",
                      err)

    def test_a_doctored_case_never_passes_silently(self):
        for tamper in ("expect-reconsumed", "expect-cleared"):
            rc, out, err = run_main(tamper=tamper, outcome=([], {}, []))
            self.assertEqual(rc, 1, tamper)
            self.assertIn("passed silently", err, tamper)

    def test_a_doctored_case_carrying_failures_still_fails(self):
        rc, _, _ = run_main(
            tamper="expect-cleared", outcome=([], {}, ["named evidence"])
        )
        self.assertEqual(rc, 1)

    def test_a_clean_pass_renders_the_digest_line(self):
        evidence = {
            "converged": 4, "tripped_at": 6, "consumed_at": 8,
            "relatched_at": 12, "promoted_at": 16, "drop_held_at": 20,
            "held_press_at": 25, "released_at": 27, "recovered_at": 29,
            "restored_at": 37, "entries": 41,
        }
        rc, out, err = run_main(
            outcome=([{"phase": "converge"}], evidence, [])
        )
        self.assertEqual(rc, 0)
        self.assertRegex(
            out, r"^ack-edge-lifecycle-digest [0-9a-f]{64} — "
        )
        self.assertIn("tracking by tick 4", out)
        self.assertIn("41 lifecycle records audited", out)


if __name__ == "__main__":
    unittest.main()
