"""The pair stage's file-discovered leg registration, unit-tested:
ci/legs.py's discover() parses each ci/legs/<name>.py file's
module-level LEG literal — never importing the leg — and returns the
legs sorted by their declared order, so the stage's order is recorded
inside the leg files themselves and adding a leg is one new file.
These tests pin today's legs to their recorded order (a new leg
joining the directory must not disturb it), refuse a .py file
carrying no LEG literal or a malformed one, refuse two legs sharing
an order, and ignore entries outside the convention by name. A leg
registering no doctored case is refused the same way — every leg's
own audit must be proven to fire, never silently absent."""
import importlib.util
import sys
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


class ScanBatchSettleBudget(unittest.TestCase):
    """The scan-batch leg's severed-batch wait budget is a *wait*, not a
    contract assertion: the leg's checks are exactness claims the tick
    counter decides (the batch lands exactly on its bound, the plant
    exactly on it, the follow-up exactly one past), and the wall clock
    only bounds how long the run waits for that to arrive. A single
    fixed figure read CI-runner contention as a violation — main runs
    37236005898 and 37273526223 both reported "the batch never
    terminated" for batches that were terminating, at ticks 188 and
    225 of 256. The budget therefore scales with the declared bound,
    under a floor, so no runner's load can decide a contract the
    counter decides exactly."""

    def setUp(self):
        path = _CI_DIR / "legs" / "scan_batch_bound.py"
        spec = importlib.util.spec_from_file_location("scan_batch_bound", path)
        self.leg = importlib.util.module_from_spec(spec)
        sys.argv = ["scan_batch_bound"]
        try:
            spec.loader.exec_module(self.leg)
        except SystemExit:
            # The module's argparse runs at import; the constants and
            # the budget helper above it are already bound.
            pass

    def test_the_budget_scales_with_the_declared_bound(self):
        for bound in (self.leg.SCAN_BATCH_BOUND, self.leg.DOCTORED_BOUND):
            budget = self.leg.batch_settle_timeout(bound)
            self.assertGreaterEqual(budget, bound * self.leg.SETTLE_S_PER_SCAN_S)
            self.assertGreaterEqual(budget, self.leg.SETTLE_MIN_S)

    def test_the_declared_bound_outwaits_the_doctored_one(self):
        """The declared 256-scan batch is the leg's worst case, so it
        carries the larger budget — a doctored run must never wait
        longer than the real one for the same contract."""
        self.assertGreater(
            self.leg.batch_settle_timeout(self.leg.SCAN_BATCH_BOUND),
            self.leg.batch_settle_timeout(self.leg.DOCTORED_BOUND),
        )

    def test_a_small_bound_still_gets_the_floor(self):
        """The allowance scales but never collapses the wait below the
        floor a settling batch needs whatever its size."""
        self.assertEqual(
            self.leg.batch_settle_timeout(1), self.leg.SETTLE_MIN_S
        )

    def test_the_budget_outlasts_the_observed_flake(self):
        """The regression the scaling answers: the reported failures
        left the tick at 188 and 225 of a 256-scan batch, and the budget
        covers the whole bound rather than a slice of it."""
        self.assertGreaterEqual(
            self.leg.batch_settle_timeout(self.leg.SCAN_BATCH_BOUND), 256
        )

    def test_the_contract_checks_are_not_the_budget(self):
        """The exactness assertions the leg exists to make are
        untouched by the budget: it still fails a batch that overruns
        its bound and one that never starts, so a longer wait cannot
        mask either."""
        source = (_CI_DIR / "legs" / "scan_batch_bound.py").read_text()
        self.assertIn("past its bound", source)
        self.assertIn("the at-bound batch never started", source)
        self.assertIn("did not land exactly its bound", source)


class DoctoredCases(unittest.TestCase):
    """Every leg plants a doctored negative — the <stem>-unchecked
    self-check proving the leg's own audit fires. A LEG record with a
    missing or empty `tampers` list is refused by name rather than
    silently weakening the stage's evidence."""

    def test_a_record_without_tampers_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            write_leg(directory, "real_leg.py", 10)
            (Path(directory) / "honest_only.py").write_text(
                'LEG = {"order": 20, "title": "x", "passes": "x"}\n'
            )
            with self.assertRaises(legs.Invalid) as raised:
                legs.discover(directory)
            self.assertIn("honest_only.py", str(raised.exception))

    def test_a_record_with_empty_tampers_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            write_leg(directory, "real_leg.py", 10)
            (Path(directory) / "honest_only.py").write_text(
                'LEG = {"order": 20, "title": "x", "passes": "x", '
                '"tampers": []}\n'
            )
            with self.assertRaises(legs.Invalid) as raised:
                legs.discover(directory)
            self.assertIn("honest_only.py", str(raised.exception))

    def test_a_tamper_missing_a_required_field_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            write_leg(directory, "real_leg.py", 10)
            (Path(directory) / "partial_tamper.py").write_text(
                'LEG = {"order": 20, "title": "x", "passes": "x", '
                '"tampers": [{"name": "doctored", "passed": "p", '
                '"missed": "m"}]}\n'
            )
            with self.assertRaises(legs.Invalid) as raised:
                legs.discover(directory)
            self.assertIn("partial_tamper.py", str(raised.exception))

    def test_a_leg_with_one_wellformed_tamper_validates(self):
        with tempfile.TemporaryDirectory() as directory:
            write_leg(directory, "real_leg.py", 10)
            path = str(Path(directory) / "real_leg.py")
            record = legs.validate_leg(path, legs.read_leg(path))
            self.assertEqual(record["stem"], "real-leg")
            self.assertEqual(len(record["tampers"]), 1)

    def test_the_real_legs_directory_discovers(self):
        discovered = legs.discover(str(_CI_DIR / "legs"))
        self.assertTrue(discovered)
        for leg in discovered:
            self.assertTrue(leg["tampers"], leg["file"])


if __name__ == "__main__":
    unittest.main()
