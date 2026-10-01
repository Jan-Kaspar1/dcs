"""The commissioning/handover record leg's declaration and audit
seams, unit-tested against faked models and records:
commissioning.ARTIFACTS names exactly the declared set
docs/releases/commissioning-record.md records (I/O checkout record,
loop-check evidence, alarm rationalization sign-off, documentation
turnover), commissioning.TAMPER_ARTIFACT maps each missing-artifact
tamper to the artifact the completeness audit must name,
commissioning.checkout_points extracts the declared channel-backed
I/O set with each point's resolved signal name,
commissioning.level_chain resolves the declared measurement loop's
driven point and threshold span, commissioning.missing_artifacts
reports the absent entries in declared order, and the tamper-driven
audit reports `the record carries no <artifact> artifact` for each
doctored case."""
import importlib.util
import sys
import unittest
from pathlib import Path

_CI_DIR = Path(__file__).resolve().parents[1] / "reference-plant" / "ci"
sys.path.insert(0, str(_CI_DIR))
_PATH = _CI_DIR / "legs" / "commissioning.py"
_spec = importlib.util.spec_from_file_location("commissioning", _PATH)
commissioning = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(commissioning)

_DOC = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "releases"
    / "commissioning-record.md"
)


def model():
    """A minimal emitted-model shape: two channel-backed I/O points
    and one internal point, a threshold chain with a declared span,
    and the signal index naming the field points — the lowest-id
    signal winning each source."""
    return {
        "io_points": [
            {
                "id": 10,
                "name": "level-primary",
                "direction": "in",
                "value_type": "float",
                "channel": {"device": "sim", "index": 0},
            },
            {
                "id": 40,
                "name": "p101-cmd",
                "direction": "out",
                "value_type": "bool",
                "channel": {"device": "sim", "index": 1},
            },
            {
                "id": 60,
                "name": "demand",
                "direction": "internal",
                "value_type": "int",
            },
        ],
        "signals": [
            {"id": 3, "name": "level-primary", "source": 10},
            {"id": 7, "name": "level-alias", "source": 10},
            {"id": 4, "name": "p101-cmd", "source": 40},
        ],
        "components": [
            {
                "id": "chain",
                "kind": "threshold-chain",
                "parameters": {
                    "cutoff": {"float": 3.0},
                    "high": {"float": 9.0},
                },
            }
        ],
    }


def full_record():
    """A record carrying every named artifact — the completeness
    audit's clean case."""
    return {
        "artifacts": {
            name: {"evidence": name}
            for name in commissioning.ARTIFACTS
        }
    }


class DeclaredSetTests(unittest.TestCase):
    """The record's declared artifact set and tamper wiring."""

    def test_artifact_set_names_the_four_declared_artifacts(self):
        self.assertEqual(
            commissioning.ARTIFACTS,
            (
                "io-checkout",
                "loop-check",
                "alarm-signoff",
                "documentation-turnover",
            ),
        )

    def test_every_tamper_drops_a_named_artifact(self):
        self.assertEqual(
            sorted(commissioning.TAMPER_ARTIFACT.values()),
            sorted(commissioning.ARTIFACTS),
        )
        for tamper, artifact in commissioning.TAMPER_ARTIFACT.items():
            self.assertEqual(tamper, f"missing-{artifact}")

    def test_procedure_names_the_run_phases_in_order(self):
        self.assertEqual(commissioning.PROCEDURE[0], "converge")
        self.assertEqual(commissioning.PROCEDURE[-1], "completeness-audit")
        self.assertIn("handover", commissioning.PROCEDURE)
        for artifact in commissioning.ARTIFACTS:
            self.assertIn(artifact, commissioning.PROCEDURE)

    def test_record_doc_names_every_artifact(self):
        doc = _DOC.read_text()
        for artifact in commissioning.ARTIFACTS:
            self.assertIn(f"`{artifact}`", doc)
        for tamper in commissioning.TAMPER_ARTIFACT:
            self.assertIn(tamper, doc)

    def test_marks_rise_and_fall_strictly_inside_the_span(self):
        fractions = commissioning.MARK_FRACTIONS
        self.assertTrue(all(0.0 < f < 1.0 for f in fractions))
        peak = fractions.index(max(fractions))
        self.assertEqual(
            list(fractions[: peak + 1]), sorted(fractions[: peak + 1])
        )
        self.assertEqual(
            list(fractions[peak:]), sorted(fractions[peak:], reverse=True)
        )


class MissingArtifactsTests(unittest.TestCase):
    """The completeness audit's findings, in declared order."""

    def test_full_record_reports_no_missing_artifacts(self):
        self.assertEqual(
            commissioning.missing_artifacts(full_record()), []
        )

    def test_absent_and_empty_artifacts_are_reported(self):
        record = full_record()
        record["artifacts"]["loop-check"] = {}
        del record["artifacts"]["alarm-signoff"]
        self.assertEqual(
            commissioning.missing_artifacts(record),
            ["loop-check", "alarm-signoff"],
        )

    def test_missing_artifacts_follow_the_declared_order(self):
        self.assertEqual(
            commissioning.missing_artifacts({"artifacts": {}}),
            list(commissioning.ARTIFACTS),
        )

    def test_each_tamper_leaves_exactly_its_named_artifact_missing(self):
        for artifact in commissioning.TAMPER_ARTIFACT.values():
            record = full_record()
            record["artifacts"].pop(artifact)
            self.assertEqual(
                commissioning.missing_artifacts(record), [artifact]
            )


class CheckoutPointsTests(unittest.TestCase):
    """The declared channel-backed I/O set the census audits."""

    def test_extracts_only_channel_backed_points_in_order(self):
        points = commissioning.checkout_points(model())
        self.assertEqual([entry["point"] for entry in points], [10, 40])

    def test_carries_direction_value_type_channel_and_name(self):
        entry = commissioning.checkout_points(model())[0]
        self.assertEqual(entry["direction"], "in")
        self.assertEqual(entry["value_type"], "float")
        self.assertEqual(
            entry["channel"], {"device": "sim", "index": 0}
        )
        # The lowest-signal-id name wins the point's resolution.
        self.assertEqual(entry["name"], "level-primary")

    def test_internal_points_never_enter_the_census(self):
        self.assertNotIn(
            60,
            {entry["point"] for entry in commissioning.checkout_points(model())},
        )


class LevelChainTests(unittest.TestCase):
    """The declared measurement loop the input leg drives."""

    def test_resolves_the_declared_point_and_span(self):
        self.assertEqual(
            commissioning.level_chain(model()), (10, 3.0, 9.0)
        )

    def test_no_chain_means_nothing_to_drive(self):
        broken = model()
        broken["components"] = []
        self.assertIsNone(commissioning.level_chain(broken))

    def test_an_undeclared_or_backwards_span_is_refused(self):
        broken = model()
        broken["components"][0]["parameters"] = {
            "cutoff": {"float": 9.0},
            "high": {"float": 3.0},
        }
        self.assertIsNone(commissioning.level_chain(broken))

    def test_a_missing_signal_means_nothing_to_drive(self):
        broken = model()
        broken["signals"] = [
            s for s in broken["signals"] if s["source"] != 10
        ]
        self.assertIsNone(commissioning.level_chain(broken))


class ReceiptedWriteTests(unittest.TestCase):
    """The output leg's receipted write body."""

    def test_write_value_carries_the_bool_point_write(self):
        self.assertEqual(
            commissioning.write_value(40, True),
            {
                "write_value": {
                    "kind": "bool",
                    "point": 40,
                    "value": {"bool": True},
                }
            },
        )


if __name__ == "__main__":
    unittest.main()
