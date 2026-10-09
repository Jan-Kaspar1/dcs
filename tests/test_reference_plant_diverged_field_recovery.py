"""The diverged_field_recovery leg's registration and boundary
coverage: ci/legs.py's discovery (parsing, never importing) finds the
leg at its declared order between the staged-vs-field divergence leg
and the standby-restart leg, the leg's doctored cases are the two the
check drives, and the run's own boundary guards — the emitted model's
carried field output and pair manifest — are the
ones the leg declares it needs. The leg's live run belongs to the
pair stage in `ci/check.sh` against the pinned release.
"""
import importlib.util
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_CI_DIR = _ROOT / "reference-plant" / "ci"
_LEG_PATH = _CI_DIR / "legs" / "diverged_field_recovery.py"


def load_leg():
    """The leg module, imported by path so the test reads its helpers
    directly without the pair-stage tooling present."""
    spec = importlib.util.spec_from_file_location(
        "diverged_field_recovery", _LEG_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RegistrationTests(unittest.TestCase):

    def test_the_leg_registers_between_divergence_and_standby_restart(self):
        spec = importlib.util.spec_from_file_location(
            "legs_driver", _CI_DIR / "legs.py")
        driver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(driver)
        names = [
            Path(leg["file"]).name
            for leg in driver.discover(str(_CI_DIR / "legs"))
        ]
        self.assertIn("diverged_field_recovery.py", names)
        self.assertLess(
            names.index("divergence.py"),
            names.index("diverged_field_recovery.py"),
        )
        self.assertLess(
            names.index("diverged_field_recovery.py"),
            names.index("standby_restart.py"),
        )

    def test_the_leg_declares_the_recorded_diagnostics(self):
        leg = load_leg()
        self.assertEqual(leg.LEG["passes"], "wedge-recovery")
        self.assertEqual(leg.LEG["failed"], "wedge-recovery-failed")
        self.assertEqual(
            [tamper["name"] for tamper in leg.LEG["tampers"]],
            ["expect-promote", "skip-relaunch"],
        )

    def test_the_leg_reports_inconclusive_rather_than_failing_a_pin(self):
        leg = load_leg()
        self.assertTrue(issubclass(leg.Inconclusive, Exception))
        # A pinned release predating the contract must classify
        # inconclusive, never a failure — the digest line names it.
        prose = (leg.__doc__ or '') + (leg.main.__doc__ or '')
        self.assertIn("wedge-recovery-digest", prose)
        self.assertIn("wedge-recovery-nondeterministic", prose)
        self.assertIn("predating", prose)

    def test_the_contract_declares_the_emitted_diagnostics(self):
        contract = (_ROOT / "docs" / "release-contract.md").read_text()
        for name in (
                "`wedge-recovery-failed`",
                "`wedge-recovery-nondeterministic`",
                "`diverged-field-recovery-unchecked`"):
            self.assertIn(name, contract)


class BoundaryTests(unittest.TestCase):

    def setUp(self):
        self.leg = load_leg()

    def test_sync_kind_reads_both_wire_shapes(self):
        self.assertEqual(self.leg.sync_kind({"sync": "unsynchronized"}),
                         "unsynchronized")
        self.assertEqual(
            self.leg.sync_kind({"sync": {"diverged": {"mismatches": []}}}),
            "diverged",
        )
        self.assertIsNone(self.leg.sync_kind({"sync": None}))
        self.assertIsNone(self.leg.sync_kind({}))

    def test_refusal_sync_reads_the_reported_verdict(self):
        refusal = {"not_converged": {"sync": {"tracking": {"aligned": 7}}}}
        self.assertEqual(self.leg.refusal_sync(refusal),
                         {"tracking": {"aligned": 7}})
        self.assertIsNone(self.leg.refusal_sync({"already_active": {}}))
        self.assertIsNone(self.leg.refusal_sync(None))

    def test_role_changes_reads_the_fenced_walk(self):
        entries = [
            {"tick": 4, "event": {"role_changed": {
                "from": "active", "to": "demoting", "origin": "fenced"}}},
            {"tick": 5, "event": {"command_settled": {}}},
        ]
        self.assertEqual(self.leg.role_changes(entries),
                         [{"from": "active", "to": "demoting",
                           "origin": "fenced"}])
        self.assertEqual(self.leg.role_changes(None), [])

    def test_a_model_without_a_carried_output_stops_the_run(self):
        import json
        import tempfile

        class Args:
            tamper = None
            manifest = ""
            model = ""

        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "plant.json"
            model.write_text(json.dumps({"signals": [], "io_points": []}))
            manifest = Path(directory) / "manifest.json"
            manifest.write_text(json.dumps(
                {"model": {"fingerprint": "0" * 64}, "controllers": [
                    {"name": "duty", "listen": "0.0.0.0:8080"},
                    {"name": "spare", "listen": "0.0.0.0:8081",
                     "standby": "duty:8080"}]}))
            Args.model = str(model)
            Args.manifest = str(manifest)
            with self.assertRaises(self.leg.Abort) as raised:
                self.leg.wedge_recovery_pass(Args(), None)
        self.assertIn("has nothing to exercise", str(raised.exception))

    def test_a_manifest_without_a_pair_stops_the_run(self):
        import json
        import tempfile

        class Args:
            tamper = None
            manifest = ""
            model = ""

        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "plant.json"
            model.write_text(json.dumps(
                {"signals": [{"name": "p101-cmd", "id": 7, "source": 7}],
                 "io_points": [{"id": 7, "direction": "out",
                                "value_type": "bool",
                                "channel": "field.7"}]}))
            manifest = Path(directory) / "manifest.json"
            manifest.write_text(json.dumps(
                {"model": {"fingerprint": "0" * 64},
                 "controllers": [{"name": "duty",
                                  "listen": "0.0.0.0:8080"}]}))
            Args.model = str(model)
            Args.manifest = str(manifest)
            with self.assertRaises(self.leg.Abort) as raised:
                self.leg.wedge_recovery_pass(Args(), None)
        self.assertIn("declares no standby pair", str(raised.exception))


if __name__ == "__main__":
    unittest.main()