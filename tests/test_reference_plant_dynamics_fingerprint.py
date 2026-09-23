"""The dynamics-fingerprint leg's document helpers, unit-tested against
the checked-in model and dynamics documents:
dynamics_fingerprint.canonical_bytes gives the parsed document a
key-order- and whitespace-insensitive canonical form so the fingerprint
is semantic — a renumbered point reference perturbs it, reformatting
never does; dynamics_fingerprint.renumbered_documents shifts every
declared point id and carries every reference — signal `source`,
connection endpoints, and the dynamics `input`/`output`/`inputs`
fields — so the doctored deployment still loads while its element set
stays identical; and dynamics_fingerprint.first_diverging_element names
the first list position the served document diverges at (element 0 for
the renumbering, None for identical documents)."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_CI_DIR = _REPO / "reference-plant" / "ci"
sys.path.insert(0, str(_CI_DIR))
_PATH = _CI_DIR / "dynamics_fingerprint.py"
_spec = importlib.util.spec_from_file_location("dynamics_fingerprint", _PATH)
dynamics_fingerprint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dynamics_fingerprint)

MODEL = _REPO / "reference-plant" / "model" / "plant.json"
DYNAMICS = _REPO / "reference-plant" / "model" / "dynamics.json"
MANIFEST = _REPO / "reference-plant" / "deploy" / "manifest.json"


def load(path):
    with open(path) as handle:
        return json.load(handle)


def renumbered():
    """The doctored pair `(model, dynamics)` parsed back from the
    scratch paths `renumbered_documents` writes, beside the pristine
    documents."""
    scratch = tempfile.mkdtemp()
    model_path, dynamics_path = dynamics_fingerprint.renumbered_documents(
        str(MODEL), str(DYNAMICS), scratch
    )
    return (load(model_path), load(dynamics_path), load(MODEL), load(DYNAMICS))


class CanonicalFingerprintTests(unittest.TestCase):
    """The fingerprint contract: semantic identity — key order and
    whitespace never perturb it, any semantic difference does, and the
    manifest's recorded `dynamics.fingerprint` equals the checked-in
    document's."""

    def test_key_order_and_whitespace_do_not_perturb_the_digest(self):
        document = load(DYNAMICS)
        pretty = json.dumps(document, indent=2).encode()
        shuffled = json.dumps(
            [{kind: dict(reversed(list(body.items())))}
             for element in document
             for kind, body in element.items()],
        ).encode()
        self.assertEqual(
            dynamics_fingerprint.fnv1a(
                dynamics_fingerprint.canonical_bytes(json.loads(pretty))
            ),
            dynamics_fingerprint.fnv1a(
                dynamics_fingerprint.canonical_bytes(json.loads(shuffled))
            ),
        )

    def test_a_renumbered_point_reference_changes_the_digest(self):
        document = load(DYNAMICS)
        body = next(iter(document[0].values()))
        body["input"] += 1
        self.assertNotEqual(
            dynamics_fingerprint.document_fingerprint(load(DYNAMICS)),
            dynamics_fingerprint.document_fingerprint(document),
        )

    def test_identical_documents_fingerprint_identically(self):
        self.assertEqual(
            dynamics_fingerprint.dynamics_fingerprint(str(DYNAMICS)),
            dynamics_fingerprint.document_fingerprint(load(DYNAMICS)),
        )

    def test_the_manifest_records_the_checked_in_fingerprint(self):
        manifest = load(MANIFEST)
        self.assertEqual(
            manifest["dynamics"]["fingerprint"],
            dynamics_fingerprint.dynamics_fingerprint(
                str(_REPO / "reference-plant" / manifest["dynamics"]["path"])
            ),
        )

    def test_the_fingerprint_is_the_sixteen_hex_shape(self):
        fingerprint = dynamics_fingerprint.dynamics_fingerprint(str(DYNAMICS))
        self.assertRegex(fingerprint, r"^[0-9a-f]{16}$")


class RenumberedDocumentsTests(unittest.TestCase):
    """The tamper's doctored deployment: every point id shifted, every
    reference carried, the element set's content untouched."""

    def test_every_declared_point_id_is_shifted(self):
        model, _dynamics, original, _orig_dynamics = renumbered()
        self.assertEqual(
            [point["id"] for point in model["io_points"]],
            [
                point["id"] + dynamics_fingerprint.POINT_SHIFT
                for point in original["io_points"]
            ],
        )

    def test_every_dynamics_point_reference_is_carried(self):
        model, dynamics, original, orig_dynamics = renumbered()
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

    def test_the_element_content_is_identical_but_for_point_ids(self):
        _model, dynamics, _original, orig_dynamics = renumbered()
        shift = dynamics_fingerprint.POINT_SHIFT
        for element, original_element in zip(dynamics, orig_dynamics):
            self.assertEqual(
                list(element.keys()), list(original_element.keys())
            )
            for body, original_body in zip(
                element.values(), original_element.values()
            ):
                for key, value in body.items():
                    original_value = original_body[key]
                    if key in ("input", "output"):
                        self.assertEqual(value, original_value + shift)
                    elif key == "inputs":
                        self.assertEqual(
                            value,
                            [point + shift for point in original_value],
                        )
                    else:
                        self.assertEqual(value, original_value)

    def test_the_doctored_dynamics_fingerprints_differently(self):
        _model, dynamics, _original, orig_dynamics = renumbered()
        self.assertNotEqual(
            dynamics_fingerprint.document_fingerprint(dynamics),
            dynamics_fingerprint.document_fingerprint(orig_dynamics),
        )

    def test_first_divergence_is_the_first_element(self):
        _model, dynamics, _original, orig_dynamics = renumbered()
        self.assertEqual(
            dynamics_fingerprint.first_diverging_element(
                orig_dynamics, dynamics
            ),
            'element 0 ("bool_flow")',
        )


class FirstDivergingElementTests(unittest.TestCase):
    """The diagnostic's element walk: first differing list position,
    the tail on a length divergence, None on identical documents."""

    def test_identical_documents_report_no_divergence(self):
        document = load(DYNAMICS)
        self.assertIsNone(
            dynamics_fingerprint.first_diverging_element(document, document)
        )

    def test_a_later_element_names_its_position_and_kind(self):
        expected = load(DYNAMICS)
        served = json.loads(json.dumps(expected))
        served[4]["integrator"]["initial"] = 0.0
        self.assertEqual(
            dynamics_fingerprint.first_diverging_element(expected, served),
            'element 4 ("integrator")',
        )

    def test_a_shorter_served_document_names_the_missing_element(self):
        expected = load(DYNAMICS)
        served = expected[:-1]
        self.assertIn(
            f"element {len(expected) - 1}",
            dynamics_fingerprint.first_diverging_element(expected, served),
        )

    def test_a_longer_served_document_names_the_added_element(self):
        expected = load(DYNAMICS)
        served = json.loads(json.dumps(expected))
        served.append(
            {"noise": {"input": 10, "output": 10, "amplitude": 0.1,
                       "initial": 0.0}}
        )
        self.assertIn(
            f"element {len(expected)}",
            dynamics_fingerprint.first_diverging_element(expected, served),
        )


if __name__ == "__main__":
    unittest.main()
