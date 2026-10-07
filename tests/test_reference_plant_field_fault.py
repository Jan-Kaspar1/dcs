"""The field_fault leg's unit coverage — ci/legs/field_fault.py is the
reference plant's consumer-pair mirror of the qa rig's field-fault leg
(`qa_lane/scenarios/3600_field_fault.py`), proving WW-OPS-003's
honest-degradation contract on the deployed redundant pair rather than
on the lane's rig. These tests pin, without launching the pair: the
leg's registration record and its three doctored cases, the
model-driven resolution of the two field inputs it faults, the
served-sample quality and `io_health` counter helpers the audit reads
(including an unreadable section never reading as a zero), the
journal projection the recovery comparison runs, and the role-hold
audit that refuses to let a field fault pass for a peer failure."""
import importlib.util
import json
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
_CI_DIR = _ROOT / "reference-plant" / "ci"
_LEG_PATH = _CI_DIR / "legs" / "field_fault.py"
_MODEL_PATH = _ROOT / "reference-plant" / "model" / "plant.json"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


legs = load(_CI_DIR / "legs.py", "legs")
leg = load(_LEG_PATH, "field_fault_leg")
model = json.loads(_MODEL_PATH.read_text())

BAD = {"bad": "device_fault"}


def snapshot(tick=40, samples=None, io_health=None):
    """A served snapshot carrying the given `(point, quality, value)`
    triples and `io_health` section."""
    points = [
        {"point": point,
         "sample": {"value": value, "quality": quality, "tick": tick}}
        for point, quality, value in samples or []
    ]
    return {"tick": tick, "points": points, "io_health": io_health}


def quality_entry(seq, tick, point, to):
    return {
        "seq": seq,
        "tick": tick,
        "event": {"quality_changed": {"point": point, "from": "good",
                                      "to": to}},
    }


class Registration(unittest.TestCase):
    """The leg's `LEG` literal — the pair stage's discovery contract:
    a unique declared order, the stem and failed diagnostic following
    the file-name convention, and one doctored case per contract
    clause, each declaring evidence the honest run prints."""

    def test_the_leg_registers_in_the_pair_stage(self):
        discovered = {
            Path(entry["file"]).name: entry
            for entry in legs.discover(str(_CI_DIR / "legs"))
        }
        record = discovered["field_fault.py"]
        self.assertEqual(record["stem"], "field-fault")
        self.assertEqual(record["passes"], "field-fault")
        # `failed` is optional in the record and defaults to the stem's
        # own diagnostic, which is the name #668's criterion fixes.
        self.assertEqual(record.get("failed", f"{record['stem']}-failed"),
                         "field-fault-failed")

    def test_the_leg_runs_inside_the_boundary_lints_glob(self):
        # The consumers stage's boundary lint covers ci/legs/*.py, so a
        # leg that names no platform-checkout path joins by file.
        source = _LEG_PATH.read_text()
        for needle in ("crates/", "../", "file://", "/home/", "target/debug"):
            self.assertNotIn(needle, source, needle)

    def test_the_doctored_cases_carry_named_evidence(self):
        tampers = {entry["name"]: entry for entry in leg.LEG["tampers"]}
        self.assertEqual(
            set(tampers),
            {"serve-good", "flat-counters", "lingering-quality"},
        )
        source = _LEG_PATH.read_text()
        for name, entry in tampers.items():
            self.assertTrue(entry["evidence"], name)
            for evidence in entry["evidence"]:
                self.assertIn(evidence, source, name)
            for field in ("passed", "missed"):
                self.assertIsInstance(entry[field], str)

    def test_every_doctored_case_is_accepted_by_the_leg(self):
        # A tamper the leg's own argument parser rejects would never
        # exercise its audit: the harness's substring check would pass
        # against a case the leg cannot run.
        source = _LEG_PATH.read_text()
        for name in ("serve-good", "flat-counters", "lingering-quality"):
            self.assertIn(name, source, name)
            self.assertIn(f'tamper == "{name}"', source, name)


class Resolution(unittest.TestCase):
    """The leg's fault targets resolve out of the emitted model — the
    declared seam, never a hard-coded point id, and a model that cannot
    declare it resolves to the abort the leg reports."""

    def test_the_emitted_model_resolves_both_field_inputs(self):
        points = leg.signal_points(model)
        self.assertIsNotNone(points)
        self.assertNotEqual(points["level_primary"], points["level_backup"])
        for point in points.values():
            self.assertIsInstance(point, int)

    def test_a_model_without_the_level_set_resolves_nothing(self):
        points = leg.signal_points({"signals": [
            {"id": 1, "name": "demand", "source": 204}]})
        self.assertIsNone(points)


class ServedReads(unittest.TestCase):
    """The audit's served reads: a quality stamp, a counter that never
    reads an absent integer as zero, and an `io_health` section whose
    absence is unreadable rather than empty."""

    def test_quality_is_read_off_the_served_sample(self):
        served = snapshot(samples=[(10, BAD, {"float": 1.5})])
        self.assertEqual(leg.quality(served, 10), BAD)
        self.assertIsNone(leg.quality(served, 11))
        self.assertIsNone(leg.quality(None, 10))

    def test_health_is_none_when_the_release_serves_no_section(self):
        self.assertIsNone(leg.health(snapshot()))
        self.assertIsNone(leg.health({"io_health": "not a section"}))
        self.assertEqual(leg.health(snapshot(io_health={"a": 1})), {"a": 1})

    def test_an_absent_counter_reads_unread_not_zero(self):
        self.assertEqual(leg.counter({"failed_reads": 0}, "failed_reads"), 0)
        self.assertIsNone(leg.counter({}, "failed_reads"))
        self.assertIsNone(leg.counter({"failed_reads": "3"},
                                      "failed_reads"))
        self.assertIsNone(leg.counter({"failed_reads": True},
                                      "failed_reads"))
        self.assertEqual(leg.counter({"failed_reads": 3}, "failed_reads"), 3)

    def test_value_comes_from_the_snapshot_point_helper(self):
        served = snapshot(samples=[(10, "good", {"float": 1.5})])
        self.assertEqual(leg.value(served, 10), {"float": 1.5})


class JournalProjection(unittest.TestCase):
    """The recovery comparison reads quality transitions out of a
    peer's durable record in `seq` order — value transitions ride the
    same projection and neither is invented."""

    def test_quality_and_value_transitions_project_in_seq_order(self):
        entries = [
            {"seq": 1, "tick": 40, "event": {
                "point_changed": {"point": 10, "from": None,
                                  "to": {"float": 1.0}}}},
            quality_entry(2, 41, 10, BAD),
            quality_entry(3, 48, 10, "good"),
        ]
        self.assertEqual(
            leg.journal_events(entries),
            [("changed", 10, {"float": 1.0}),
             ("quality", 10, BAD),
             ("quality", 10, "good")],
        )

    def test_an_empty_record_projects_nothing(self):
        self.assertEqual(leg.journal_events([]), [])
        self.assertEqual(leg.journal_events(None), [])
        self.assertEqual(leg.quality_events({"duty": []}, "duty"), [])

    def test_only_quality_transitions_feed_the_recovery_comparison(self):
        events = {"duty": [("changed", 10, {"float": 1.0}),
                           ("quality", 10, "good")]}
        self.assertEqual(leg.quality_events(events, "duty"),
                         [("quality", 10, "good")])


class RoleHold(unittest.TestCase):
    """The role audit: a field fault is not a peer failure, so a moved
    role inside the staged window is a failure the leg reports rather
    than a note."""

    class _Rig:
        duty_url = "http://duty"
        standby_url = "http://standby"

    def _audit(self, duty, standby):
        answers = {
            "http://duty/role": duty,
            "http://standby/role": standby,
        }
        with mock.patch.object(
            leg.pair, "get",
            side_effect=lambda url, what, failures: answers[url],
        ):
            failures = []
            reports = leg.roles_hold(self._Rig(), failures,
                                     "under the fault")
        # The audit holds when it recorded no failure; the reports it
        # answers with are the two role reads it polled.
        return reports, failures

    def test_the_launch_roles_hold(self):
        reports, failures = self._audit(
            {"role": "active", "tick": 40},
            {"role": "standby", "sync": {"tracking": {"aligned": 40}}},
        )
        self.assertEqual(failures, [])
        self.assertEqual([report["role"] for report in reports],
                         ["active", "standby"])

    def test_a_demoted_owner_under_a_field_fault_is_a_failure(self):
        _reports, failures = self._audit(
            {"role": "standby", "tick": 40},
            {"role": "standby", "sync": {"tracking": {"aligned": 40}}},
        )
        self.assertEqual(len(failures), 1)
        self.assertIn("launch roles did not hold", failures[0])

    def test_a_peer_that_stopped_tracking_is_a_failure(self):
        _reports, failures = self._audit(
            {"role": "active", "tick": 40},
            {"role": "standby", "sync": {"degraded": {"reason": "miss"}}},
        )
        self.assertEqual(len(failures), 1)


class DriveUntil(unittest.TestCase):
    """The driven-tick walk: the bound names a transition that never
    came, and the satisfying snapshot is what the audit reads."""

    class _Rig:
        duty_url = "http://duty"
        standby_url = "http://standby"

        def __init__(self, snapshots):
            self.snapshots = list(snapshots)
            self.scanned = 0

        def tick(self, tracked, owner, failures):
            # The pair rig's tick answers the tracking peer's snapshot
            # beside the field owner's, so each driven step scans the
            # tracking peer first.
            self.scanned += 1
            served = self.snapshots[min(self.scanned - 1,
                                        len(self.snapshots) - 1)]
            return dict(served), served

    def test_it_returns_the_first_satisfying_snapshot(self):
        rig = self._Rig([
            {"tick": 40, "points": [{"point": 10, "sample": {
                "value": {"float": 1.0}, "quality": "good",
                "tick": 40}}]},
            {"tick": 41, "points": [{"point": 10, "sample": {
                "value": {"float": 1.0}, "quality": BAD,
                "tick": 41}}]},
        ])
        seen = leg.drive_until(
            rig, [], lambda snapshot: leg.quality(snapshot, 10) == BAD,
            bound=4)
        self.assertEqual(seen["tick"], 41)
        self.assertEqual(rig.scanned, 2)

    def test_an_unarriving_transition_reads_none(self):
        rig = self._Rig([{"tick": 40, "points": []}])
        seen = leg.drive_until(
            rig, [], lambda snapshot: leg.quality(snapshot, 10) == BAD,
            bound=3)
        self.assertIsNone(seen)
        self.assertEqual(rig.scanned, 3)
