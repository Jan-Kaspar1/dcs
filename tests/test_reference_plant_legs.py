"""The pair stage's file-discovered leg registration, unit-tested:
ci/legs.py's discover() parses each ci/legs/<name>.py file's
module-level LEG literal — never importing the leg — and returns the
legs sorted by their declared order, so the stage's order is recorded
inside the leg files themselves and adding a leg is one new file.
These tests pin today's legs to their recorded order (a new leg
joining the directory must not disturb it), refuse a .py file
carrying no LEG literal or a malformed one, refuse two legs sharing
an order, and ignore entries outside the convention by name."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

_CI_DIR = Path(__file__).resolve().parents[1] / "reference-plant" / "ci"
_PATH = _CI_DIR / "legs.py"
_spec = importlib.util.spec_from_file_location("legs", _PATH)
legs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(legs)

# The recorded order — the sequence today's pair stage runs its legs
# in, held from before the legs became file-discovered. A new leg
# registers by choosing a free order in its own file; this list pins
# the existing legs' relative order without needing an edit per leg.
RECORDED_ORDER = [
    "pair",
    "negotiation",
    "startup_claim",
    "refusal",
    "handover",
    "takeover",
    "force_carryover",
    "tune_carryover",
    "force_release",
    "stale_checkpoint",
    "burst_order",
    "peer_announce",
    "availability",
    "failover",
    "divergence",
    "standby_restart",
    "report",
    "command_switch",
    "demote_pending",
    "demote_reconvergence",
    "managed_lifecycle",
    "event_parity",
    "monitor_starvation",
    "commissioning",
    "journal_boundary",
]

_LEG = '''\
LEG = {{
    "order": {order},
    "title": "the {stem} leg",
    "passes": "{stem}-leg",
    "tampers": [
        {{
            "name": "doctored",
            "passed": "a doctored case passed the {stem} leg",
            "missed": "the doctored case did not report its diagnostic",
            "evidence": ["named diagnostic"],
        }},
    ],
}}
'''


def write_leg(directory, name, order):
    (Path(directory) / name).write_text(_LEG.format(order=order, stem=name[:-3]))


class DiscoveryOrder(unittest.TestCase):
    def test_todays_legs_run_in_the_recorded_order(self):
        discovered = legs.discover(str(_CI_DIR / "legs"))
        names = [Path(leg["file"]).name for leg in discovered]
        positions = []
        for recorded in RECORDED_ORDER:
            self.assertIn(
                recorded + ".py", names, f"{recorded} is not discovered"
            )
            positions.append(names.index(recorded + ".py"))
        self.assertEqual(
            positions,
            sorted(positions),
            "the recorded legs' relative order changed",
        )
        self.assertEqual(
            len(names), len(set(names)), "a leg file is discovered twice"
        )

    def test_discovery_orders_by_the_declared_order_not_the_name(self):
        with tempfile.TemporaryDirectory() as directory:
            write_leg(directory, "zzz_first.py", 10)
            write_leg(directory, "aaa_second.py", 20)
            names = [
                Path(leg["file"]).name for leg in legs.discover(directory)
            ]
            self.assertEqual(names, ["zzz_first.py", "aaa_second.py"])

    def test_a_non_leg_file_is_ignored_by_name(self):
        with tempfile.TemporaryDirectory() as directory:
            write_leg(directory, "only_leg.py", 10)
            (Path(directory) / "notes.txt").write_text("not a leg")
            (Path(directory) / "README.md").write_text("not a leg")
            (Path(directory) / "helpers").mkdir()
            (Path(directory) / "helpers" / "x.py").write_text("x = 1")
            names = [
                Path(leg["file"]).name for leg in legs.discover(directory)
            ]
            self.assertEqual(names, ["only_leg.py"])

    def test_a_leg_file_without_a_leg_literal_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            write_leg(directory, "real_leg.py", 10)
            (Path(directory) / "forgotten.py").write_text("x = 1\n")
            with self.assertRaises(legs.Invalid) as raised:
                legs.discover(directory)
            self.assertIn("forgotten.py", str(raised.exception))

    def test_a_non_literal_leg_record_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            write_leg(directory, "real_leg.py", 10)
            (Path(directory) / "computed.py").write_text(
                "LEG = dict(order=20, title='x', passes='x')\n"
            )
            with self.assertRaises(legs.Invalid) as raised:
                legs.discover(directory)
            self.assertIn("computed.py", str(raised.exception))

    def test_a_record_missing_a_required_field_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            write_leg(directory, "real_leg.py", 10)
            (Path(directory) / "partial.py").write_text(
                'LEG = {"order": 20}\n'
            )
            with self.assertRaises(legs.Invalid) as raised:
                legs.discover(directory)
            self.assertIn("partial.py", str(raised.exception))

    def test_two_legs_sharing_an_order_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            write_leg(directory, "first.py", 10)
            write_leg(directory, "second.py", 10)
            with self.assertRaises(legs.Invalid) as raised:
                legs.discover(directory)
            self.assertIn("first.py", str(raised.exception))
            self.assertIn("second.py", str(raised.exception))

    def test_the_stem_is_the_file_name_with_dashes(self):
        self.assertEqual(legs.leg_stem("demote_pending.py"), "demote-pending")
        self.assertEqual(legs.leg_stem("pair.py"), "pair")


if __name__ == "__main__":
    unittest.main()
