"""The nonfinite_refusal leg's unit coverage — ci/legs/
nonfinite_refusal.py is the reference plant's consumer-boundary
mirror of the qa rig's nonfinite-refusal scenario leg (#1021's
mirror of #993's 0750_nonfinite_refusal.py), proving the #834/#866
input-validation contract on the released pair. These tests pin,
without launching the pair: the leg's registration record and its
released dcs-plant-ctl tool flag, the refusal-name and claim-verdict
classification the wire's error envelope takes, the finite-float and
poisoned-census scans the served frame's representability contract
rests on, the model-driven resolution of the driven point off the
emitted artifact, the raw-line request the non-finite spellings
need, the shipped-tool invocation classification, and the
inconclusive / doctored-case classifications the harness relies on —
the new leg's evidence lines and inconclusive handling asserted as
the issue requires."""
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
_LEG_PATH = _CI_DIR / "legs" / "nonfinite_refusal.py"
_MODEL_PATH = _ROOT / "reference-plant" / "model" / "plant.json"
_DYNAMICS_PATH = _ROOT / "reference-plant" / "model" / "dynamics.json"
_MANIFEST_PATH = _ROOT / "reference-plant" / "deploy" / "manifest.json"
_SCENARIO_PATH = _CI_DIR / "scenario.json"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


legs = load(_CI_DIR / "legs.py", "legs")
leg = load(_LEG_PATH, "nonfinite_refusal")


def model():
    with open(_MODEL_PATH) as handle:
        return json.load(handle)


def dynamics():
    with open(_DYNAMICS_PATH) as handle:
        return json.load(handle)


def argv(tamper=None):
    args = [
        "nonfinite_refusal.py",
        "--plant-server", "/nonexistent/dcs-plant-server",
        "--controller", "/nonexistent/dcs-controller",
        "--plant-ctl", "/nonexistent/dcs-plant-ctl",
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
    or the exception nonfinite_refusal_pass raises. Returns
    `(rc, stdout, stderr)`."""
    if isinstance(outcome, BaseException):
        stub = mock.Mock(side_effect=outcome)
    else:
        stub = mock.Mock(return_value=outcome)
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", argv(tamper)), \
            mock.patch.object(leg, "nonfinite_refusal_pass", stub), \
            contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        rc = leg.main()
    return rc, out.getvalue(), err.getvalue()


def point_entry(point, value, direction="in", tick=3):
    """A census entry shaped like the plant's `list_points` serves."""
    return {
        "point": point,
        "direction": direction,
        "sample": {"value": value, "quality": "good", "tick": tick},
    }


def census(values):
    """The points list for `probe_point` — `{point: value-dict}` —
    shaped like the plant's `list_points` serves."""
    return [point_entry(point, value) for point, value in values.items()]


class Registration(unittest.TestCase):
    """The leg's `LEG` literal — the pair stage's discovery contract:
    the declared order is unique across the directory, the stem and
    failed diagnostic follow the file-name convention, the released
    dcs-plant-ctl ships as the leg's declared tool, and the doctored
    case declares the evidence the honest run reports."""

    def test_the_leg_registers_in_the_pair_stage(self):
        discovered = {
            Path(entry["file"]).name: entry
            for entry in legs.discover(str(_CI_DIR / "legs"))
        }
        record = discovered["nonfinite_refusal.py"]
        self.assertEqual(record["stem"], "nonfinite-refusal")
        self.assertEqual(record["order"], 310)
        # No "failed" override — the default <stem>-failed is the
        # issue's named diagnostic.
        self.assertNotIn("failed", record)
        self.assertEqual(record["tools"], {"plant-ctl": "dcs-plant-ctl"})

    def test_the_doctored_case_carries_named_evidence(self):
        tampers = {entry["name"]: entry for entry in leg.LEG["tampers"]}
        self.assertEqual(set(tampers), {"expect-applied"})
        # The declared evidence must be the diagnostic the leg
        # actually prints — pin it against the source's failure lines
        # so a drifted message cannot pass the harness's substring
        # check by accident.
        source = _LEG_PATH.read_text()
        for name, entry in tampers.items():
            self.assertTrue(entry["evidence"], name)
            for evidence in entry["evidence"]:
                self.assertIn(evidence, source, name)
            for field in ("passed", "missed"):
                self.assertIsInstance(entry[field], str)


class Resolution(unittest.TestCase):
    """The driven point resolves off the emitted model, the dynamics'
    element outputs, and the served census — the leg exercises the
    declared junction, never a hard-coded id: `inflow`, the forcing
    input, first, then the `net-flow` element junction, then any
    finite float in-point, `undriven` reporting whether the dynamics
    leaves the resolved point unwritten."""

    def test_the_emitted_model_names_the_declared_points(self):
        named = leg.named_sources(model())
        self.assertEqual(named["inflow"][1], 12)
        self.assertEqual(named["net-flow"][1], 13)

    def test_the_dynamics_outputs_cover_the_driven_points(self):
        self.assertEqual(
            leg.element_outputs(dynamics()),
            {10, 11, 12, 13, 15, 20, 21},
        )
        self.assertEqual(leg.element_outputs({"elements": []}), set())
        self.assertEqual(leg.element_outputs([]), set())

    def test_the_forcing_input_resolves_first(self):
        point, undriven = leg.probe_point(
            model(),
            census({12: {"float": 0.0}, 13: {"float": 0.2}}),
            set(),
        )
        self.assertEqual((point, undriven), (12, True))

    def test_an_element_driven_point_resolves_driven(self):
        point, undriven = leg.probe_point(
            model(),
            census({12: {"float": 0.0}, 13: {"float": 0.2}}),
            leg.element_outputs(dynamics()),
        )
        self.assertEqual((point, undriven), (12, False))

    def test_an_unserved_forcing_input_falls_to_the_junction(self):
        point, undriven = leg.probe_point(
            model(),
            census({12: {"float": None}, 13: {"float": 0.2}}),
            set(),
        )
        self.assertEqual((point, undriven), (13, True))

    def test_any_finite_float_in_point_serves_as_fallback(self):
        point, undriven = leg.probe_point(
            {"signals": []},
            census({10: {"float": None}, 11: {"float": 0.8}}),
            {11},
        )
        self.assertEqual((point, undriven), (11, False))

    def test_an_out_direction_point_never_resolves(self):
        entries = [
            point_entry(12, {"float": 0.0}, direction="out"),
        ]
        self.assertEqual(
            leg.probe_point(model(), entries, set()), (None, False)
        )

    def test_no_finite_float_resolves_none(self):
        self.assertEqual(
            leg.probe_point(
                model(), census({12: {"bool": True}}), set()
            ),
            (None, False),
        )
        self.assertEqual(
            leg.probe_point(model(), [], set()), (None, False)
        )


class WireShapes(unittest.TestCase):
    """The refusal-name and claim-verdict classification the plant
    protocol's `{"result":"error","error":…}` envelope takes, the
    served Value's finite-float decode, and the poisoned-census
    scan for the `{"float":null}` family."""

    def test_the_strict_decode_refusal_names_invalid_request(self):
        answer = {
            "result": "error",
            "error": {"kind": "invalid_request",
                      "detail": "cannot parse the request line"},
        }
        self.assertEqual(leg.refusal_name(answer), "invalid_request")
        self.assertIsNone(leg.claim_verdict(answer))

    def test_the_value_level_refusal_names_io_invalid_value(self):
        answer = {
            "result": "error",
            "error": {"kind": "io",
                      "error": {"invalid_value": {"point": 12}}},
        }
        self.assertEqual(leg.refusal_name(answer), "io.invalid_value")

    def test_an_off_contract_answer_names_nothing(self):
        answer = {"result": "error",
                  "error": {"kind": "io",
                            "error": {"timeout": 12}}}
        self.assertIsNone(leg.refusal_name(answer))
        self.assertIsNone(leg.refusal_name({"result": "done"}))
        self.assertIsNone(leg.refusal_name(None))

    def test_the_claim_verdicts_name_their_kinds(self):
        for kind in ("fenced", "unclaimed"):
            answer = {"result": "error",
                      "error": {"kind": kind, "detail": "…"}}
            self.assertEqual(leg.claim_verdict(answer), kind)
        io_fenced = {"result": "error",
                     "error": {"kind": "io", "error": {"fenced": 12}}}
        self.assertEqual(leg.claim_verdict(io_fenced), "fenced")
        self.assertIsNone(
            leg.claim_verdict(
                {"result": "error",
                 "error": {"kind": "invalid_request"}}
            )
        )

    def test_finite_float_accepts_only_a_finite_float(self):
        self.assertEqual(leg.finite_float({"float": 1.5}), 1.5)
        self.assertEqual(leg.finite_float({"float": 0}), 0)
        self.assertIsNone(leg.finite_float({"float": None}))
        self.assertIsNone(leg.finite_float({"float": True}))
        self.assertIsNone(leg.finite_float({"int": 3}))
        self.assertIsNone(leg.finite_float({"bool": False}))
        self.assertIsNone(leg.finite_float(None))

    def test_the_census_scan_finds_only_the_poisoned_entries(self):
        entries = [
            point_entry(10, {"float": 0.8}),
            point_entry(11, {"float": None}),
            point_entry(12, {"bool": True}),
        ]
        self.assertEqual(leg.nonfinite_samples(entries), [entries[1]])
        self.assertEqual(leg.nonfinite_samples(entries[:1]), [])


class ToolSurface(unittest.TestCase):
    """The shipped `dcs-plant-ctl` invocations: a printed answer
    decodes only on a zero exit, and a refusal classifies `parse`
    when the tool's own argument boundary answers, `wire` when the
    field's does."""

    def test_a_successful_invocation_decodes_its_answer(self):
        invocation = (
            0,
            '{"result": "sample", "sample": {"value": {"float": 1.0}}}',
            "",
        )
        payload = leg.ctl_payload(invocation)
        self.assertEqual(payload["result"], "sample")
        self.assertIsNone(leg.ctl_refusal_name(invocation))

    def test_a_refused_invocation_yields_no_payload(self):
        self.assertIsNone(leg.ctl_payload((1, "", "refused")))
        self.assertIsNone(leg.ctl_payload((0, "not json", "")))

    def test_the_parse_boundary_names_parse(self):
        invocation = (
            1, "",
            'invalid value "1e999": expected true|false, an integer, '
            "or a float\nusage: dcs-plant-ctl …",
        )
        self.assertEqual(leg.ctl_refusal_name(invocation), "parse")
        invocation = (
            1, "",
            'invalid step "1e999": expected a finite number\nusage: …',
        )
        self.assertEqual(leg.ctl_refusal_name(invocation), "parse")

    def test_a_server_carried_refusal_names_wire(self):
        invocation = (
            1, "",
            "dcs-plant-ctl: 127.0.0.1:4700: field mutation refused: "
            "another attachment owns field writes",
        )
        self.assertEqual(leg.ctl_refusal_name(invocation), "wire")

    def test_a_transport_failure_names_other(self):
        invocation = (
            1, "",
            "dcs-plant-ctl: cannot reach the plant server at "
            "127.0.0.1:4700: connection refused",
        )
        self.assertEqual(leg.ctl_refusal_name(invocation), "other")


class RawRequest(unittest.TestCase):
    """The raw-line round trip — the `1e999` spellings `json.dumps`
    cannot emit reach the wire verbatim, and the answer line decodes
    the same as `PlantClient.request`'s."""

    def test_the_payload_reaches_the_wire_verbatim(self):
        class Stream:
            def __init__(self):
                self.written = []

            def write(self, data):
                self.written.append(data)

            def flush(self):
                pass

            def readline(self):
                return '{"error": {"kind": "invalid_request"}}\n'

        stream = Stream()
        client = SimpleNamespace(stream=stream)
        payload = '{"op":"write","point":12,"value":{"float":1e999}}'
        answer = leg.raw_request(client, payload)
        self.assertEqual(stream.written, [payload + "\n"])
        self.assertEqual(
            answer, {"error": {"kind": "invalid_request"}}
        )


class Inconclusive(unittest.TestCase):
    """The release-precedence classification: a rig predating the
    shared-claim or input-validation contract raises the leg's
    Inconclusive, which main renders as a stable digest line and a
    zero exit — and a doctored case over an inconclusive run still
    fails, carrying the doctored expectation's named evidence."""

    def test_main_reports_the_inconclusive_digest(self):
        rc, out, err = run_main(
            outcome=leg.Inconclusive(
                "the launched active recorded no owner-token claim "
                "line — the pinned release predates the shared-claim "
                "seam"
            )
        )
        self.assertEqual(rc, 0)
        self.assertIn(
            "nonfinite-refusal-digest inconclusive — the launched "
            "active recorded no owner-token claim line",
            out,
        )
        self.assertIn("nonfinite-refusal: inconclusive", err)

    def test_two_inconclusive_passes_render_identically(self):
        first = run_main(outcome=leg.Inconclusive("pre-contract"))
        second = run_main(outcome=leg.Inconclusive("pre-contract"))
        self.assertEqual(first[0], 0)
        self.assertEqual(first[1], second[1])

    def test_a_doctored_case_over_an_inconclusive_run_fails(self):
        rc, out, err = run_main(
            tamper="expect-applied",
            outcome=leg.Inconclusive("pre-contract"),
        )
        self.assertEqual(rc, 1)
        self.assertIn(
            "the doctored expectation wanted the payload applied", err
        )


class Classification(unittest.TestCase):
    """main()'s exit classification over the pass result — failures
    stream to stderr prefixed by the stem, a doctored case may never
    exit zero, and a clean pass renders the sha digest line."""

    def test_failures_report_by_name(self):
        rc, out, err = run_main(
            outcome=([], {}, ["the +inf write was applied"])
        )
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn(
            "nonfinite-refusal: the +inf write was applied", err
        )

    def test_an_abort_reports_by_name(self):
        rc, out, err = run_main(
            outcome=leg.Abort("the pair never converged")
        )
        self.assertEqual(rc, 1)
        self.assertIn(
            "nonfinite-refusal: the pair never converged", err
        )

    def test_a_doctored_case_never_passes_silently(self):
        rc, out, err = run_main(
            tamper="expect-applied", outcome=([], {}, [])
        )
        self.assertEqual(rc, 1)
        self.assertIn("passed silently", err)

    def test_a_doctored_case_carrying_failures_still_fails(self):
        rc, _, _ = run_main(
            tamper="expect-applied",
            outcome=(
                [], {},
                ["the +inf write answered refused — the doctored "
                 "expectation wanted the payload applied"],
            ),
        )
        self.assertEqual(rc, 1)

    def test_a_clean_pass_renders_the_digest_line(self):
        evidence = {
            "converged": 4,
            "point": 12,
            "stepped_to": 6,
            "final_tick": 7,
        }
        rc, out, err = run_main(
            outcome=([{"phase": "converge"}], evidence, [])
        )
        self.assertEqual(rc, 0)
        self.assertRegex(
            out, r"^nonfinite-refusal-digest [0-9a-f]{64} — "
        )
        self.assertIn("tracking by tick 4", out)
        self.assertIn("point 12", out)
        self.assertIn("plant to tick 6", out)
        self.assertIn("finite through tick 7", out)


if __name__ == "__main__":
    unittest.main()
