"""The in-service revision-roll leg's declaration and doctoring
seams, unit-tested against faked models: revision_roll.LEG
registers the leg with its two doctored cases, the held, added, and
spare point ids are distinct and sit outside every declared block,
and revision_roll.emit_incompatible retypes the held point under its
declared identity while rewiring its port onto the spare — so the
document still validates and only the carryover rule refuses the
crossing, naming the point."""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

_CI_DIR = Path(__file__).resolve().parents[1] / "reference-plant" / "ci"
sys.path.insert(0, str(_CI_DIR))
_PATH = _CI_DIR / "legs" / "revision_roll.py"
_spec = importlib.util.spec_from_file_location("revision_roll", _PATH)
revision_roll = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(revision_roll)


def document():
    """A minimal revision-2 shape: the held bool point wired to a
    port, plus an unrelated field point."""
    return {
        "io_points": [
            {
                "id": 240,
                "direction": "in",
                "value_type": "bool",
                "writable": True,
                "initial": {"bool": False},
            },
            {
                "id": 10,
                "direction": "in",
                "value_type": "float",
                "channel": {"device": "sim", "index": 0},
            },
        ],
        "signals": [],
        "connections": [
            {
                "from": {"point": 240},
                "to": {"port": {"component": 51, "name": "run"}},
            }
        ],
    }


class LegRegistrationTests(unittest.TestCase):
    """The leg's stage registration and point-id scheme."""

    def test_leg_registers_after_the_existing_legs(self):
        self.assertEqual(revision_roll.LEG["order"], 892)
        self.assertEqual(
            revision_roll.LEG["title"], "the in-service revision-roll leg"
        )
        self.assertEqual(revision_roll.LEG["passes"], "revision-roll-leg")

    def test_tampers_name_their_evidence(self):
        tampers = {entry["name"]: entry for entry in revision_roll.LEG["tampers"]}
        self.assertEqual(
            set(tampers), {"unarmed-revision", "incompatible-model"}
        )
        for entry in tampers.values():
            self.assertTrue(entry["evidence"])
        self.assertIn("degraded", tampers["unarmed-revision"]["evidence"])
        self.assertIn(
            "internal point 240", tampers["incompatible-model"]["evidence"]
        )

    def test_point_ids_are_distinct_and_below_the_alarm_blocks(self):
        held = revision_roll.HELD_POINT
        note = revision_roll.REVISION_NOTE
        spare = revision_roll.SPARE_POINT
        self.assertEqual(len({held, note, spare}), 3)
        for point in (held, note, spare):
            self.assertLess(point, 1000)


class IncompatibleDoctoringTests(unittest.TestCase):
    """The deliberately incompatible revision's shape."""

    def write_revision(self, directory, doc=None):
        path = Path(directory) / "model-revision-2.json"
        path.write_text(json.dumps(doc if doc is not None else document()))
        return path

    def test_retype_keeps_identity_and_rewires_the_port(self):
        with tempfile.TemporaryDirectory() as scratch:
            self.write_revision(scratch)
            broken = revision_roll.emit_incompatible(scratch)
            doctored = json.loads(Path(broken).read_text())
        points = {point["id"]: point for point in doctored["io_points"]}
        held = points[240]
        self.assertEqual(held["value_type"], "int")
        self.assertEqual(held["initial"], {"int": 0})
        spare = points[246]
        self.assertEqual(spare["value_type"], "bool")
        self.assertEqual(
            [
                connection["from"].get("point")
                for connection in doctored["connections"]
            ],
            [246],
        )

    def test_spare_signal_sources_the_spare_point(self):
        with tempfile.TemporaryDirectory() as scratch:
            self.write_revision(scratch)
            broken = revision_roll.emit_incompatible(scratch)
            doctored = json.loads(Path(broken).read_text())
        spares = [
            signal
            for signal in doctored["signals"]
            if signal["source"] == 246
        ]
        self.assertEqual(len(spares), 1)

    def test_missing_held_point_is_refused(self):
        doc = document()
        doc["io_points"] = [
            point for point in doc["io_points"] if point["id"] != 240
        ]
        with tempfile.TemporaryDirectory() as scratch:
            self.write_revision(scratch, doc)
            with self.assertRaises(Exception) as raised:
                revision_roll.emit_incompatible(scratch)
        self.assertIn("240", str(raised.exception))

    def test_ambiguous_rewire_is_refused(self):
        doc = document()
        doc["connections"] = []
        with tempfile.TemporaryDirectory() as scratch:
            self.write_revision(scratch, doc)
            with self.assertRaises(Exception) as raised:
                revision_roll.emit_incompatible(scratch)
        self.assertIn("240", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
