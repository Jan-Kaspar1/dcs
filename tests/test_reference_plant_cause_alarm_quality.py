"""The cause_alarm_quality leg's unit coverage — ci/legs/
cause_alarm_quality.py is the reference plant's consumer-boundary
mirror of the qa rig's quality-aware cause-alarm scenario (#1486's
consolidated #827/#871/#1134/#1424 family), proving the
quality-aware cause-alarm contract on the released pair. These tests
pin, without launching the pair: the leg's registration record, its
model-driven resolution of the declared quality-gated protection seam
out of the emitted artifact, the inconclusive classifications for a
model that declares no such surface and for a pinned release predating
the contract, the journal lifecycle projection and its ordered audit,
the receipted command bodies the run submits, and main()'s exit
classification — including the doctored cases the <leg>-unchecked
self-check relies on."""
import contextlib
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
_CI_DIR = _ROOT / "reference-plant" / "ci"
_LEG_PATH = _CI_DIR / "legs" / "cause_alarm_quality.py"
_MODEL_PATH = _ROOT / "reference-plant" / "model" / "plant.json"
_MANIFEST_PATH = _ROOT / "reference-plant" / "deploy" / "manifest.json"
_SCENARIO_PATH = _CI_DIR / "scenario.json"

sys.path.insert(0, str(_CI_DIR / "legs"))
sys.path.insert(0, str(_CI_DIR))


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


legs = load(_CI_DIR / "legs.py", "legs")
simulate = load(_CI_DIR / "simulate.py", "simulate")
leg = load(_LEG_PATH, "cause_alarm_quality")

TRUE, FALSE = {"bool": True}, {"bool": False}
BAD = {"bad": "device_fault"}

POINTS = {
    "mode": 300,
    "hand": 301,
    "cmd": 100,
    "avail": 321,
    "protect": 324,
    "protect_ok": 326,
    "fault": 312,
    "fault_alarm": 1063,
    "fault_unack": 1064,
    "fault_shelved": 1065,
    "fault_suppressed": 1066,
    "fault_out_of_service": 1067,
    "thermal": 60,
    "thermal_ack": 1070,
    "thermal_alarm": 1073,
    "thermal_unack": 1074,
    "thermal_shelved": 1075,
    "thermal_suppressed": 1076,
    "thermal_out_of_service": 1077,
    "moisture": 80,
    "moisture_ack": 1080,
    "moisture_alarm": 1083,
    "moisture_unack": 1084,
    "moisture_shelved": 1085,
    "moisture_suppressed": 1086,
    "moisture_out_of_service": 1087,
}
SEAM = {
    "thermal": {"alarm": "managed-bool-latching-alarm:29",
                "guard": "interlock:23", "in": 1119},
    "moisture": {"alarm": "managed-bool-latching-alarm:30",
                 "guard": "interlock:24", "in": 1121},
}


def model():
    with open(_MODEL_PATH) as handle:
        return json.load(handle)


def points():
    """The leg's resolved point map — the artifact's own ids."""
    resolved = dict(POINTS)
    for kind in leg.CONTACTS:
        resolved[f"{kind}_alarm_in"] = SEAM[kind]["in"]
    return resolved


def argv(tamper=None):
    args = [
        "cause_alarm_quality.py",
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
    """`main()` against a stubbed pass — `outcome` the return value or
    the exception cause_pass raises. Returns `(rc, stdout, stderr)`."""
    if isinstance(outcome, BaseException):
        stub = mock.Mock(side_effect=outcome)
    else:
        stub = mock.Mock(return_value=outcome)
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", argv(tamper)), \
            mock.patch.object(leg, "cause_pass", stub), \
            contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        rc = leg.main()
    return rc, out.getvalue(), err.getvalue()


def served_schema(document=None, drop=(), raw_contact=None):
    """A served block-interface registry shaped document built from the
    emitted model's own registry expectations — the shape `GET /schema`
    serves. `drop` omits named components; `raw_contact` rebinds a
    component's `in` port to a raw field point."""
    document = model() if document is None else document
    schema = {"interfaces": []}
    for name, want in simulate.registry_expectations(document).items():
        if name in drop:
            continue
        interface = {"kind": want["kind"], "version": 1,
                     "measurements": [], "state": [],
                     "configuration": [], "commands": []}
        for port_name, port in want["ports"].items():
            entry = dict(port, name=port_name)
            if raw_contact is not None and name == raw_contact:
                entry["point"] = POINTS["thermal"]
            collection = ("measurements" if port["direction"] == "in"
                          else "state")
            interface[collection].append(entry)
        schema["interfaces"].append({"name": name, "interface": interface})
    return schema


def snapshot(values):
    """A served snapshot carrying the named `{point: bool}` samples at
    Good quality."""
    return {
        "tick": 12,
        "points": [
            {"point": point,
             "sample": {"value": {"bool": value}, "quality": "good",
                        "tick": 12}}
            for point, value in sorted(values.items())
        ],
    }


def changed(seq, point, to):
    return {
        "seq": seq,
        "tick": seq,
        "event": {"point_changed": {"point": point, "from": None,
                                    "to": to}},
    }


def quality(seq, point, to):
    return {
        "seq": seq,
        "tick": seq,
        "event": {"quality_changed": {"point": point, "from": None,
                                      "to": to}},
    }


def settled_entry(seq, point, value, outcome="applied"):
    receipt = {
        "command": {"write_value": {"kind": "bool", "point": point,
                                    "value": value}},
        "outcome": {outcome: {"tick": seq}},
        "actor": leg.ACTOR,
    }
    return {
        "seq": seq,
        "tick": seq,
        "event": {"command_settled": {"receipt": receipt}},
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
        record = discovered["cause_alarm_quality.py"]
        self.assertEqual(record["stem"], "cause-alarm-quality")
        self.assertEqual(record["order"], 860)
        self.assertEqual(record["title"],
                         "the quality-aware cause-alarm leg")
        self.assertEqual(record["passes"], "cause-alarm-quality")
        self.assertEqual(record.get("failed", f"{record['stem']}-failed"),
                         "cause-alarm-quality-failed")

    def test_the_doctored_cases_carry_named_evidence(self):
        tampers = {entry["name"]: entry for entry in leg.LEG["tampers"]}
        self.assertEqual(
            set(tampers), {"expect-silent", "expect-standing"}
        )
        source = _LEG_PATH.read_text()
        for name, entry in tampers.items():
            self.assertTrue(entry["evidence"], name)
            for evidence in entry["evidence"]:
                self.assertIn(evidence, source, name)
            for field in ("passed", "missed"):
                self.assertIsInstance(entry[field], str)


class Resolution(unittest.TestCase):
    """The leg's seam resolution out of the emitted artifact: the
    declared signals, the cause guards the alarms ride, and the
    receipted drive points — a model that cannot declare the surface
    reports the named inconclusive instead of passing vacuously."""

    def test_the_emitted_model_resolves_the_leg_points(self):
        self.assertEqual(leg.signal_points(model()), POINTS)

    def test_the_emitted_model_resolves_the_cause_guards(self):
        self.assertEqual(
            leg.quality_gated_seam(model(), POINTS), SEAM
        )

    def test_the_emitted_model_declares_the_receipted_seam(self):
        self.assertEqual(
            leg.receipted_seam(model(), POINTS), POINTS
        )

    def test_a_missing_signal_resolves_no_points(self):
        document = model()
        document["signals"] = [
            signal for signal in document["signals"]
            if signal["name"] != "p101-moisture-alarm"
        ]
        self.assertIsNone(leg.signal_points(document))

    def test_an_unwritable_ack_reports_inconclusive(self):
        document = model()
        for point in document["io_points"]:
            if point["id"] == POINTS["thermal_ack"]:
                point["writable"] = False
        with self.assertRaises(leg.Inconclusive) as raised:
            leg.receipted_seam(document, POINTS)
        self.assertIn("not declared writable", str(raised.exception))
        self.assertIn("thermal ack", str(raised.exception))

    def test_no_managed_alarm_binding_is_inconclusive(self):
        document = model()
        document["components"] = [
            component for component in document["components"]
            if component.get("kind") not in leg.MANAGED_KINDS
        ]
        with self.assertRaises(leg.Inconclusive) as raised:
            leg.quality_gated_seam(document, POINTS)
        self.assertIn("binds no managed cause alarm",
                      str(raised.exception))

    def test_a_raw_contact_condition_is_inconclusive(self):
        # The pre-#827 shape: the alarm's condition binds the contact
        # itself, so no guard is declared between them.
        document = model()
        for index, connection in enumerate(document["connections"]):
            port = connection.get("to", {}).get("port", {})
            if port.get("name") != "in" \
                    or port.get("component") != 29:
                continue
            served = connection["from"]
            if "port" in served:
                document["connections"][index] = {
                    "from": {"point": POINTS["thermal"]},
                    "to": {"port": port},
                }
        with self.assertRaises(leg.Inconclusive) as raised:
            leg.quality_gated_seam(document, POINTS)
        self.assertIn("no quality-gated protection contact",
                      str(raised.exception))

    def test_a_guard_misreading_its_contact_is_inconclusive(self):
        document = model()
        for connection in document["connections"]:
            port = connection.get("to", {}).get("port", {})
            if port.get("component") == 23 \
                    and port.get("name") == "trip_1":
                connection["from"] = {"point": POINTS["moisture"]}
        with self.assertRaises(leg.Inconclusive) as raised:
            leg.quality_gated_seam(document, POINTS)
        self.assertIn("binds no raw contact",
                      str(raised.exception))

    def test_an_unjournaled_contact_is_inconclusive(self):
        document = model()
        for point in document["io_points"]:
            if point["id"] == POINTS["moisture"]:
                point["journaled"] = False
        with self.assertRaises(leg.Inconclusive) as raised:
            leg.quality_gated_seam(document, POINTS)
        self.assertIn("not a journaled field Bool input",
                      str(raised.exception))


class ContractProbe(unittest.TestCase):
    """The release-precedence probe: the served block-interface
    registry must report each cause guard and bind the cause alarm's
    condition to the guard's carrier, never to the raw contact."""

    def test_a_registry_serving_the_guards_probes_true(self):
        self.assertTrue(
            leg.contract_probe(served_schema(), SEAM, POINTS)
        )

    def test_a_registry_predating_the_contract_probes_false(self):
        schema = served_schema(drop=("interlock:23",))
        self.assertFalse(leg.contract_probe(schema, SEAM, POINTS))

    def test_a_registry_binding_the_raw_contact_probes_false(self):
        # The release serving the pre-#827 binding: the cause alarm's
        # condition on the raw contact, with no guard reported at all.
        schema = served_schema(
            drop=("interlock:23", "interlock:24"),
            raw_contact="managed-bool-latching-alarm:29",
        )
        self.assertFalse(leg.contract_probe(schema, SEAM, POINTS))

    def test_an_empty_registry_probes_false(self):
        self.assertFalse(leg.contract_probe({}, SEAM, POINTS))
        self.assertFalse(
            leg.contract_probe({"interfaces": []}, SEAM, POINTS)
        )


class AlarmSurfaces(unittest.TestCase):
    """The declared annunciation predicates the drives assert: the
    condition, `alarm`, the latch as asked, and the managed outputs
    down — plus the honest-absence half's sibling and motor-fault
    checks."""

    def standing(self, **keys):
        """`(resolved points, served snapshot)` with every declared
        point false and the named leg keys standing."""
        resolved = points()
        values = {point: False for point in resolved.values()}
        for key, wanted in keys.items():
            values[resolved[key]] = wanted
        return resolved, snapshot(values)

    def test_the_declared_annunciation_holds(self):
        resolved, snap = self.standing(
            thermal_alarm_in=True, thermal_alarm=True, thermal_unack=True
        )
        self.assertTrue(
            leg.alarm_standing(snap, resolved, "thermal", True)
        )
        self.assertFalse(
            leg.alarm_standing(snap, resolved, "thermal", False)
        )

    def test_a_managed_output_standing_refuses_the_annunciation(self):
        resolved, snap = self.standing(
            thermal_alarm_in=True, thermal_alarm=True, thermal_unack=True,
            thermal_suppressed=True,
        )
        self.assertFalse(
            leg.alarm_standing(snap, resolved, "thermal", True)
        )

    def test_the_reported_clear_holds_with_the_latch_standing(self):
        resolved, snap = self.standing(thermal_unack=True)
        self.assertTrue(leg.alarm_clear(snap, resolved, "thermal", True))
        self.assertFalse(leg.alarm_clear(snap, resolved, "thermal", False))

    def test_a_standing_condition_refuses_the_reported_clear(self):
        resolved, snap = self.standing(thermal_alarm_in=True)
        self.assertFalse(leg.alarm_clear(snap, resolved, "thermal", False))

    def test_nothing_else_holds_on_the_declared_contract(self):
        resolved, snap = self.standing(
            thermal_alarm_in=True, thermal_alarm=True, thermal_unack=True
        )
        self.assertTrue(leg.nothing_else(snap, resolved, "thermal"))

    def test_nothing_else_refuses_a_cross_annunciation(self):
        resolved, snap = self.standing(
            thermal_alarm_in=True, thermal_alarm=True, thermal_unack=True,
            moisture_alarm_in=True, moisture_alarm=True, moisture_unack=True,
        )
        self.assertTrue(
            leg.alarm_standing(snap, resolved, "thermal", True)
        )
        self.assertFalse(leg.nothing_else(snap, resolved, "thermal"))

    def test_nothing_else_refuses_the_motor_fault_alarm(self):
        resolved, snap = self.standing(
            thermal_alarm_in=True, thermal_alarm=True, thermal_unack=True,
            fault_alarm=True,
        )
        self.assertFalse(leg.nothing_else(snap, resolved, "thermal"))

    def test_nothing_else_refuses_the_motor_fault_flag(self):
        resolved, snap = self.standing(
            thermal_alarm_in=True, thermal_alarm=True, thermal_unack=True,
            fault=True,
        )
        self.assertFalse(leg.nothing_else(snap, resolved, "thermal"))

    def test_proven_reads_the_quality_with_the_value(self):
        resolved, snap = self.standing(thermal=True)
        self.assertTrue(leg.proven(snap, resolved["thermal"]))
        for entry in snap["points"]:
            if entry["point"] == resolved["thermal"]:
                entry["sample"]["quality"] = BAD
        self.assertFalse(leg.proven(snap, resolved["thermal"]))

    def test_quality_of_reports_the_served_stamp(self):
        resolved, snap = self.standing()
        self.assertEqual(
            leg.quality_of(snap, resolved["thermal"]), "good"
        )
        for entry in snap["points"]:
            if entry["point"] == resolved["thermal"]:
                entry["sample"]["quality"] = BAD
        self.assertEqual(leg.quality_of(snap, resolved["thermal"]), BAD)
        self.assertIsNone(leg.quality_of(snap, 999999))


class JournalProjection(unittest.TestCase):
    """The journal audit stream: `journal_events` projects the
    settlements, the journaled value transitions and the quality
    transitions in seq order, and `ordered_group_misses` names any
    transition missing or out of run order."""

    def test_settles_changes_and_qualities_project_in_seq_order(self):
        entries = [
            quality(1, POINTS["thermal"], BAD),
            changed(2, POINTS["protect"], TRUE),
            changed(3, POINTS["protect_ok"], FALSE),
            changed(4, POINTS["thermal_alarm"], TRUE),
            changed(5, POINTS["thermal_unack"], TRUE),
            quality(6, POINTS["thermal"], "good"),
            changed(7, POINTS["protect"], FALSE),
            changed(8, POINTS["thermal_alarm"], FALSE),
            settled_entry(9, POINTS["thermal_ack"], TRUE),
            changed(10, POINTS["thermal_unack"], FALSE),
            settled_entry(11, POINTS["thermal_ack"], FALSE),
        ]
        self.assertEqual(
            leg.journal_events(entries),
            [
                ("quality", POINTS["thermal"], BAD),
                ("changed", POINTS["protect"], TRUE),
                ("changed", POINTS["protect_ok"], FALSE),
                ("changed", POINTS["thermal_alarm"], TRUE),
                ("changed", POINTS["thermal_unack"], TRUE),
                ("quality", POINTS["thermal"], "good"),
                ("changed", POINTS["protect"], FALSE),
                ("changed", POINTS["thermal_alarm"], FALSE),
                ("settled", POINTS["thermal_ack"], TRUE, "applied",
                 leg.ACTOR),
                ("changed", POINTS["thermal_unack"], FALSE),
                ("settled", POINTS["thermal_ack"], FALSE, "applied",
                 leg.ACTOR),
            ],
        )

    def test_a_rejected_settle_projects_its_reason(self):
        entries = [
            {
                "seq": 1,
                "tick": 1,
                "event": {"command_settled": {"receipt": {
                    "command": {"write_value": {
                        "kind": "bool", "point": POINTS["thermal_ack"],
                        "value": TRUE}},
                    "outcome": {"rejected": {"reason": {
                        "not_active": {}}}},
                    "actor": leg.ACTOR}}},
            }
        ]
        self.assertEqual(
            leg.journal_events(entries),
            [("settled", POINTS["thermal_ack"], TRUE, "not_active",
              leg.ACTOR)],
        )

    def test_the_ordered_audit_accepts_run_order(self):
        entries = [
            quality(1, POINTS["thermal"], BAD),
            changed(2, POINTS["protect"], TRUE),
            changed(3, POINTS["thermal_alarm"], TRUE),
            changed(4, POINTS["thermal"], TRUE),
            changed(5, POINTS["thermal_alarm"], FALSE),
        ]
        self.assertEqual(
            leg.ordered_group_misses(
                leg.journal_events(entries),
                [
                    [("quality", POINTS["thermal"], BAD),
                     ("changed", POINTS["protect"], TRUE),
                     ("changed", POINTS["thermal_alarm"], TRUE)],
                    [("changed", POINTS["thermal"], TRUE),
                     ("changed", POINTS["thermal_alarm"], FALSE)],
                ],
            ),
            [],
        )

    def test_the_ordered_audit_names_a_missing_transition(self):
        entries = [quality(1, POINTS["thermal"], BAD)]
        (miss,) = leg.ordered_group_misses(
            leg.journal_events(entries),
            [[("quality", POINTS["thermal"], BAD),
              ("changed", POINTS["thermal_alarm"], TRUE)]],
        )
        self.assertIn(str(POINTS["thermal_alarm"]), miss)
        self.assertIn("group 0", miss)

    def test_the_ordered_audit_names_an_out_of_order_transition(self):
        # The latch recorded before the trip that caused it: the
        # audit's forward cursor can no longer reach it.
        entries = [
            changed(1, POINTS["thermal_unack"], TRUE),
            changed(2, POINTS["thermal"], TRUE),
        ]
        (miss,) = leg.ordered_group_misses(
            leg.journal_events(entries),
            [[("changed", POINTS["thermal"], TRUE)],
             [("changed", POINTS["thermal_unack"], TRUE)]],
        )
        self.assertIn("out of order", miss)
        self.assertIn("group 1", miss)


class ReceiptMatching(unittest.TestCase):
    """The adopted receipt log's positional matching — identical
    submissions settle once each, refusals never count as applies."""

    def command(self, point=POINTS["thermal_ack"], value=True):
        return leg.write_value(point, value)

    def receipt(self, outcome, command=None):
        return {"command": command or self.command(),
                "outcome": outcome, "actor": leg.ACTOR}

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
            self.receipt({"applied": {"tick": 3}},
                         self.command(POINTS["thermal_ack"], False))
        ]
        self.assertEqual(leg.settled(receipts, self.command()), [])


class Inconclusive(unittest.TestCase):
    """The absence classification: a model or release that cannot
    declare the contract raises the leg's Inconclusive, which main
    renders as a stable digest line and a zero exit — and a doctored
    case over an inconclusive run still fails, since it can name no
    evidence."""

    def test_a_model_without_the_seam_resolves_no_surface(self):
        self.assertIsNone(leg.signal_points({}))

    def test_a_model_without_the_cause_guard_raises_inconclusive(self):
        with self.assertRaises(leg.Inconclusive) as raised:
            leg.quality_gated_seam(
                {"io_points": [], "components": [], "connections": []},
                {"thermal_alarm": 1073},
            )
        self.assertIn("binds no managed cause alarm",
                      str(raised.exception))

    def test_main_reports_the_inconclusive_digest(self):
        rc, out, err = run_main(
            outcome=leg.Inconclusive(
                "the pinned release predates the quality-aware "
                "cause-alarm contract"
            )
        )
        self.assertEqual(rc, 0)
        self.assertIn(
            "cause-alarm-quality-digest inconclusive — the pinned "
            "release predates",
            out,
        )
        self.assertIn("cause-alarm-quality: inconclusive", err)

    def test_two_inconclusive_passes_render_identically(self):
        first = run_main(outcome=leg.Inconclusive("pre-contract"))
        second = run_main(outcome=leg.Inconclusive("pre-contract"))
        self.assertEqual(first[0], 0)
        self.assertEqual(first[1], second[1])

    def test_a_doctored_case_over_an_inconclusive_run_fails(self):
        # The declared tamper evidence names the honest run's message;
        # an inconclusive run offers the doctored case no evidence, so
        # main names the doctored expectation it could not refute.
        for tamper, doctored in (
            ("expect-silent",
             "the doctored expectation wanted the cause alarm to stay "
             "silent"),
            ("expect-standing",
             "the doctored expectation wanted the field command to "
             "stand"),
        ):
            rc, out, err = run_main(
                tamper=tamper,
                outcome=leg.Inconclusive("pre-contract"),
            )
            self.assertEqual(rc, 1, tamper)
            self.assertIn(doctored, err, tamper)


class Classification(unittest.TestCase):
    """main()'s exit classification over the pass result — failures
    stream to stderr prefixed by the stem, a doctored case may never
    exit zero, and a clean pass renders the sha digest line."""

    def test_failures_report_by_name(self):
        rc, out, err = run_main(outcome=([], {}, ["the alarm stayed silent"]))
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("cause-alarm-quality: the alarm stayed silent", err)

    def test_an_abort_reports_by_name(self):
        rc, out, err = run_main(
            outcome=leg.Abort("the pair never converged")
        )
        self.assertEqual(rc, 1)
        self.assertIn("cause-alarm-quality: the pair never converged", err)

    def test_a_doctored_case_never_passes_silently(self):
        for tamper in ("expect-silent", "expect-standing"):
            rc, out, err = run_main(tamper=tamper, outcome=([], {}, []))
            self.assertEqual(rc, 1, tamper)
            self.assertIn("passed silently", err, tamper)

    def test_a_doctored_case_carrying_failures_still_fails(self):
        rc, _, _ = run_main(
            tamper="expect-silent",
            outcome=([], {}, ["expected the cause alarm to stay silent"]),
        )
        self.assertEqual(rc, 1)

    def test_a_clean_pass_renders_the_digest_line(self):
        evidence = {
            "converged": 4, "hand_held": 9, "thermal_degraded_at": 13,
            "thermal_recovered_at": 19, "moisture_degraded_at": 23,
            "moisture_recovered_at": 29, "value_trip_at": 33,
            "value_cleared_at": 40, "stopped_at": 43,
            "stopped_degraded_at": 46, "stopped_recovered_at": 55,
            "restored_at": 58, "entries": 64, "seam": SEAM,
        }
        rc, out, err = run_main(
            outcome=([{"phase": "converge"}], evidence, [])
        )
        self.assertEqual(rc, 0)
        self.assertRegex(
            out, r"^cause-alarm-quality-digest [0-9a-f]{64} — "
        )
        self.assertIn("tracking by tick 4", out)
        self.assertIn("the degraded thermal annunciated at tick 13", out)
        self.assertIn("64 journal entries", out)


if __name__ == "__main__":
    unittest.main()