"""The fingerprint leg's document helpers, unit-tested against the
checked-in model and dynamics documents:
fingerprint.renumbered_documents shifts every declared point id and
carries every reference — signal `source`, connection endpoints, and
the dynamics `input`/`output`/`inputs` fields — so the doctored
deployment still loads while its component set stays byte-identical,
and fingerprint.first_diverging_section names the first top-level
section the served document diverges in (`io_points` for the
renumbering, None for identical documents)."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_CI_DIR = _REPO / "reference-plant" / "ci"
sys.path.insert(0, str(_CI_DIR))
_PATH = _CI_DIR / "fingerprint.py"
_spec = importlib.util.spec_from_file_location("fingerprint", _PATH)
fingerprint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fingerprint)

MODEL = _REPO / "reference-plant" / "model" / "plant.json"
DYNAMICS = _REPO / "reference-plant" / "model" / "dynamics.json"


def load(path):
    with open(path) as handle:
        return json.load(handle)


def renumbered():
    """The doctored pair `(model, dynamics)` parsed back from the
    scratch paths `renumbered_documents` writes, beside the pristine
    documents."""
    scratch = tempfile.mkdtemp()
    model_path, dynamics_path = fingerprint.renumbered_documents(
        str(MODEL), str(DYNAMICS), scratch
    )
    return (load(model_path), load(dynamics_path), load(MODEL), load(DYNAMICS))


class RenumberedDocumentsTests(unittest.TestCase):
    """The tamper's doctored deployment: every point id shifted, every
    reference carried, the component set untouched."""

    def test_every_declared_point_id_is_shifted(self):
        model, _dynamics, original, _orig_dynamics = renumbered()
        self.assertEqual(
            [point["id"] for point in model["io_points"]],
            [
                point["id"] + fingerprint.POINT_SHIFT
                for point in original["io_points"]
            ],
        )

    def test_every_point_reference_is_carried(self):
        model, dynamics, original, _orig_dynamics = renumbered()
        shifted = {point["id"] for point in model["io_points"]}
        for signal in model["signals"]:
            self.assertIn(signal["source"], shifted)
        for connection in model["connections"]:
            for end in (connection["from"], connection["to"]):
                if "point" in end:
                    self.assertIn(end["point"], shifted)
        for element in dynamics:
            for body in element.values():
                for field in ("input", "output"):
                    value = body.get(field)
                    if isinstance(value, int) and not isinstance(value, bool):
                        self.assertIn(value, shifted)
                for point in body.get("inputs") or []:
                    self.assertIn(point, shifted)
        # The components section is verbatim — the silent divergence
        # the tamper exists to prove the check names anyway.
        self.assertEqual(model["components"], original["components"])

    def test_first_divergence_is_the_io_points_section(self):
        model, _dynamics, original, _orig_dynamics = renumbered()
        self.assertEqual(
            fingerprint.first_diverging_section(original, model), "io_points"
        )


class FirstDivergingSectionTests(unittest.TestCase):
    """The diagnostic's section walk: first differing top-level key in
    the fingerprinted artifact's document order, then served-only
    keys, None on identical documents."""

    def test_identical_documents_report_no_divergence(self):
        document = load(MODEL)
        self.assertIsNone(
            fingerprint.first_diverging_section(document, document)
        )

    def test_a_signals_only_change_names_signals(self):
        expected = load(MODEL)
        served = json.loads(json.dumps(expected))
        served["signals"][0]["description"] = "doctored"
        self.assertEqual(
            fingerprint.first_diverging_section(expected, served), "signals"
        )

    def test_a_missing_section_names_the_section(self):
        expected = load(MODEL)
        served = json.loads(json.dumps(expected))
        del served["devices"]
        self.assertEqual(
            fingerprint.first_diverging_section(expected, served), "devices"
        )

    def test_a_served_only_section_names_the_section(self):
        expected = load(MODEL)
        served = json.loads(json.dumps(expected))
        served["extra"] = True
        self.assertEqual(
            fingerprint.first_diverging_section(expected, served), "extra"
        )


if __name__ == "__main__":
    unittest.main()
