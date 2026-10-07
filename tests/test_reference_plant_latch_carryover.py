"""The latch_carryover leg's unit coverage — ci/legs/
latch_carryover.py is the reference plant's pair-stage proof that a
standing managed alarm's unacknowledged latch carries across a
redundant promotion (#596: WW-ENG-003, WW-LCM-001, WW-ALM-001). These
tests pin, without launching the pair: the leg's registration record,
its model-driven resolution of the power-fail latch seam off the
emitted artifact, the journal activation count and audit stream the
carryover assertion reads, and the doctored-case classifications the
harness relies on."""
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
_LEG_PATH = _CI_DIR / "legs" / "latch_carryover.py"
_MODEL_PATH = _ROOT / "reference-plant" / "model" / "plant.json"
_MANIFEST_PATH = _ROOT / "reference-plant" / "deploy" / "manifest.json"
_SCENARIO_PATH = _CI_DIR / "scenario.json"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


legs = load(_CI_DIR / "legs.py", "legs")
leg = load(_LEG_PATH, "latch_carryover")

TRUE, FALSE = {"bool": True}, {"bool": False}
POINTS = {
    "power_fail": 14,
    "power_ack": 1050,
    "power_alarm": 1053,
    "power_unack": 1054,
}


def model():
    with open(_MODEL_PATH) as handle:
        return json.load(handle)


def argv(tamper=None):
    args = [
        "latch_carryover.py",
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
    or the exception carryover_pass raises. Returns
    `(rc, stdout, stderr)`."""
    if isinstance(outcome, BaseException):
        stub = mock.Mock(side_effect=outcome)
    else:
        stub = mock.Mock(return_value=outcome)
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", argv(tamper)), \
            mock.patch.object(leg, "carryover_pass", stub), \
            contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        rc = leg.main()
    return rc, out.getvalue(), err.getvalue()


def settled_entry(seq, tick, point, value, outcome, actor="ci-latch-carryover"):
    receipt = {
        "command": {
            "write_value": {
                "kind": "bool",
                "point": point,
                "value": value,
            }
        },
        "outcome": outcome,
        "actor": actor,
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
        record = discovered["latch_carryover.py"]
        self.assertEqual(record["stem"], "latch-carryover")
        self.assertEqual(record["order"], 115)

    def test_the_doctored_cases_carry_named_evidence(self):
        tampers = {entry["name"]: entry for entry in leg.LEG["tampers"]}
        self.assertEqual(
            set(tampers), {"lost-latch", "rejournaled-activation"}
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
    """The latch seam's point map resolves out of the emitted model —
    the leg exercises the declared seam, never a hard-coded id, and a
    model that cannot declare the seam resolves to no surface."""

    def test_the_emitted_model_resolves_the_latch_points(self):
        self.assertEqual(leg.signal_points(model()), POINTS)

    def test_an_unwritable_ack_resolves_no_surface(self):
        doc = model()
        for point in doc["io_points"]:
            if point["id"] == POINTS["power_ack"]:
                point["writable"] = False
        self.assertIsNone(leg.signal_points(doc))

    def test_an_unjournaled_contact_resolves_no_surface(self):
        doc = model()
        for point in doc["io_points"]:
            if point["id"] == POINTS["power_fail"]:
                point["journaled"] = False
        self.assertIsNone(leg.signal_points(doc))

    def test_a_missing_signal_resolves_no_surface(self):
        doc = model()
        doc["signals"] = [
            signal for signal in doc["signals"]
            if signal["name"] != "power-fail-ack"
        ]
        self.assertIsNone(leg.signal_points(doc))


class ActivationAudit(unittest.TestCase):
    """The journal activation count and audit stream: `activations`
    counts the alarm point's journaled transitions to true in seq
    order, and `journal_events` projects the settled receipts and
    value transitions the carryover assertion reads."""

    def test_activations_count_the_true_transitions_in_order(self):
        entries = [
            changed_entry(1, 1, POINTS["power_fail"], TRUE),
            changed_entry(2, 2, POINTS["power_alarm"], TRUE, FALSE),
            changed_entry(3, 3, POINTS["power_unack"], TRUE, FALSE),
            changed_entry(4, 9, POINTS["power_alarm"], FALSE, TRUE),
            changed_entry(5, 12, POINTS["power_alarm"], TRUE, FALSE),
        ]
        self.assertEqual(
            leg.activations(entries, POINTS["power_alarm"]), [2, 12]
        )

    def test_a_carried_latch_adds_no_activation(self):
        entries = [
            changed_entry(1, 1, POINTS["power_alarm"], TRUE, FALSE),
            {"seq": 2, "tick": 5, "event": {"role_changed": {
                "from": "standby", "to": "promoting"}}},
            {"seq": 3, "tick": 6, "event": {"role_changed": {
                "from": "promoting", "to": "active"}}},
        ]
        # The switch journals only its role transitions — the
        # carried latch re-journals no activation.
        self.assertEqual(
            leg.activations(entries, POINTS["power_alarm"]), [1]
        )

    def test_settles_and_changes_project_in_seq_order(self):
        entries = [
            changed_entry(1, 1, POINTS["power_alarm"], TRUE),
            settled_entry(
                2, 3, POINTS["power_ack"], TRUE,
                {"applied": {"tick": 3}},
            ),
            changed_entry(3, 3, POINTS["power_unack"], FALSE, TRUE),
        ]
        self.assertEqual(
            leg.journal_events(entries),
            [
                ("changed", POINTS["power_alarm"], TRUE),
                ("settled", POINTS["power_ack"], TRUE, "applied",
                 "ci-latch-carryover"),
                ("changed", POINTS["power_unack"], FALSE),
            ],
        )

    def test_the_attributed_ack_projects_with_its_actor(self):
        entries = [
            settled_entry(
                1, 2, POINTS["power_ack"], TRUE,
                {"applied": {"tick": 2}}, actor="someone-else",
            ),
        ]
        (event,) = leg.journal_events(entries)
        self.assertEqual(event[3], "applied")
        self.assertEqual(event[4], "someone-else")


class ReceiptMatching(unittest.TestCase):
    """The adopted receipt log's settlement match — the ack write
    settles applied under the leg's actor; any other outcome, actor,
    or write never matches."""

    def command(self):
        return leg.write_value(POINTS["power_ack"], TRUE)

    def receipt(self, outcome, actor="ci-latch-carryover"):
        return {"command": self.command(), "outcome": outcome,
                "actor": actor}

    def test_the_attributed_apply_matches(self):
        receipts = [self.receipt({"applied": {"tick": 3}})]
        self.assertTrue(leg.settled(receipts, self.command()))

    def test_a_rejection_never_matches(self):
        receipts = [
            self.receipt({"rejected": {"reason": {"not_active": {}}}}),
            self.receipt({"accepted": {"apply_tick": 4}}),
        ]
        self.assertFalse(leg.settled(receipts, self.command()))

    def test_a_foreign_actor_never_matches(self):
        receipts = [
            self.receipt({"applied": {"tick": 3}}, actor="someone-else")
        ]
        self.assertFalse(leg.settled(receipts, self.command()))

    def test_a_different_write_never_matches(self):
        receipts = [
            {"command": leg.write_value(POINTS["power_ack"], FALSE),
             "outcome": {"applied": {"tick": 3}},
             "actor": "ci-latch-carryover"},
        ]
        self.assertFalse(leg.settled(receipts, self.command()))


class Classification(unittest.TestCase):
    """main()'s exit classification over the pass result — failures
    stream to stderr prefixed by the stem, a doctored case may never
    exit zero, and a clean pass renders the sha digest line."""

    def test_failures_report_by_name(self):
        rc, out, err = run_main(outcome=([], {}, ["the latch lied"]))
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("latch-carryover: the latch lied", err)

    def test_an_abort_reports_by_name(self):
        rc, out, err = run_main(
            outcome=leg.Abort("the pair never converged")
        )
        self.assertEqual(rc, 1)
        self.assertIn("latch-carryover: the pair never converged", err)

    def test_a_doctored_case_never_passes_silently(self):
        for tamper in ("lost-latch", "rejournaled-activation"):
            rc, out, err = run_main(tamper=tamper, outcome=([], {}, []))
            self.assertEqual(rc, 1, tamper)
            self.assertIn("passed silently", err, tamper)

    def test_a_doctored_case_carrying_failures_still_fails(self):
        rc, _, _ = run_main(
            tamper="lost-latch", outcome=([], {}, ["named evidence"])
        )
        self.assertEqual(rc, 1)

    def test_a_clean_pass_renders_the_digest_line(self):
        evidence = {
            "converged": 4, "tripped_at": 6, "promoted_at": 10,
            "acknowledged_at": 14, "returned_at": 20,
            "restored_at": 28,
        }
        rc, out, err = run_main(
            outcome=([{"phase": "converge"}], evidence, [])
        )
        self.assertEqual(rc, 0)
        self.assertRegex(
            out, r"^latch-carryover-digest [0-9a-f]{64} — "
        )
        self.assertIn("tracking by tick 4", out)
        self.assertIn("promoted at tick 10", out)


if __name__ == "__main__":
    unittest.main()
