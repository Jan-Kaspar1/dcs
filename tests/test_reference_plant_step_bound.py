"""The step_bound leg's unit coverage — ci/legs/step_bound.py is the
reference plant's consumer-boundary mirror of the qa rig's step-bound
scenario leg (#1178's mirror of #1177's 0760_step_bound.py), proving
the bounded step-dt contract on the released pair. These tests pin,
without launching the pair: the leg's registration record and its
released dcs-plant-ctl tool flag, the bound-refusal classification the
wire's error envelope takes and the shared helper bindings it rides
rather than forks, the driven point's resolution off the emitted
artifact, the shipped-tool invocation classification, and the
inconclusive / doctored-case classifications the harness relies on —
the new leg's evidence lines and inconclusive handling asserted as the
issue requires."""
import contextlib
import importlib
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
_LEG_PATH = _CI_DIR / "legs" / "step_bound.py"
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
# The leg rides the nonfinite-refusal leg's helpers by importing that
# module off the legs directory — the same way the pair stage runs it.
# Importing the sibling here first puts that one module object in
# sys.modules, so the identity assertions below compare the very
# functions the leg binds.
sys.path.insert(0, str(_CI_DIR / "legs"))
nonfinite_refusal = importlib.import_module("nonfinite_refusal")
leg = load(_LEG_PATH, "step_bound")


def model():
    with open(_MODEL_PATH) as handle:
        return json.load(handle)


def dynamics():
    with open(_DYNAMICS_PATH) as handle:
        return json.load(handle)


def argv(tamper=None):
    args = [
        "step_bound.py",
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
    or the exception step_bound_pass raises. Returns
    `(rc, stdout, stderr)`."""
    if isinstance(outcome, BaseException):
        stub = mock.Mock(side_effect=outcome)
    else:
        stub = mock.Mock(return_value=outcome)
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", argv(tamper)), \
            mock.patch.object(leg, "step_bound_pass", stub), \
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


def census(values, tick=3):
    """The points list the leg compares — shaped like the plant's
    `list_points` serves."""
    return [point_entry(point, value, tick=tick) for point, value in values.items()]


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
        record = discovered["step_bound.py"]
        # The file-name convention is what makes the issue's named
        # diagnostics fall out verbatim: `step-bound-failed`,
        # `step-bound-nondeterministic`, `step-bound-unchecked`.
        self.assertEqual(record["stem"], "step-bound")
        self.assertEqual(record["order"], 325)
        # No "failed" override — the default <stem>-failed is the
        # issue's named diagnostic.
        self.assertNotIn("failed", record)
        self.assertEqual(record["passes"], "step-bound-leg")
        self.assertEqual(record["tools"], {"plant-ctl": "dcs-plant-ctl"})

    def test_it_follows_the_nonfinite_refusal_leg(self):
        orders = {
            Path(entry["file"]).name: entry["order"]
            for entry in legs.discover(str(_CI_DIR / "legs"))
        }
        self.assertLess(
            orders["nonfinite_refusal.py"], orders["step_bound.py"]
        )

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


class SharedSeam(unittest.TestCase):
    """The helpers the leg rides rather than forks — the emitted
    model's signal names, the dynamics' element outputs, the driven
    point's resolution, the finite-float decode, the poisoned-sample
    scan, the claim verdicts, and the shipped-tool invocation surface —
    bound to the nonfinite-refusal leg's own, so the two legs cannot
    drift apart on what a refusal or a poisoned frame is."""

    def test_the_sibling_helpers_are_the_nonfinite_refusal_ones(self):
        for name in (
            "named_sources",
            "element_outputs",
            "probe_point",
            "finite_float",
            "nonfinite_samples",
            "claim_verdict",
            "plant_ctl",
            "ctl_payload",
        ):
            self.assertIs(
                getattr(leg, name),
                getattr(nonfinite_refusal, name),
                name,
            )

    def test_the_documented_bound_rides_the_probes(self):
        self.assertEqual(leg.BOUND, 1.0e6)
        self.assertEqual(leg.BOUND_DETAIL, "1000000")
        self.assertGreater(leg.OVER_BOUND_DT, leg.BOUND)
        self.assertGreater(leg.HUGE_DT, leg.OVER_BOUND_DT)
        self.assertEqual(leg.OVER_BOUND_DT, 1.0e7)
        self.assertEqual(leg.HUGE_DT, 1.0e308)


class BoundRefusal(unittest.TestCase):
    """The refusal classification the wire's error envelope takes: the
    documented bound named in the detail, the kind alone, an applied
    step (how a pre-contract release is recognized), and an off-contract
    answer."""

    def test_the_named_bound_refusal_is_named_bound(self):
        answer = {
            "result": "error",
            "error": {
                "kind": "invalid_request",
                "detail": "step dt must be finite, non-negative, and at"
                          " most 1000000, got 1e+308",
            },
        }
        self.assertEqual(leg.bound_refusal(answer), "bound")
        self.assertIsNone(leg.claim_verdict(answer))

    def test_a_refusal_without_the_named_bound_still_refuses(self):
        # A release whose wording differs is refused, not failed: the
        # kind is the contract's name for a bad dt.
        answer = {
            "result": "error",
            "error": {"kind": "invalid_request",
                      "detail": "step dt is out of range"},
        }
        self.assertEqual(leg.bound_refusal(answer), "invalid_request")

    def test_an_applied_step_is_no_refusal(self):
        self.assertIsNone(
            leg.bound_refusal({"result": "stepped", "tick": 9})
        )
        self.assertIsNone(leg.bound_refusal(None))

    def test_an_off_contract_error_is_no_refusal(self):
        answer = {
            "result": "error",
            "error": {"kind": "io", "error": {"timeout": 12}},
        }
        self.assertIsNone(leg.bound_refusal(answer))

    def test_the_claim_verdicts_are_not_bound_refusals(self):
        for kind in ("fenced", "unclaimed"):
            answer = {"result": "error",
                      "error": {"kind": kind, "detail": "…"}}
            self.assertIsNone(leg.bound_refusal(answer))
            self.assertEqual(leg.claim_verdict(answer), kind)
        io_fenced = {"result": "error",
                     "error": {"kind": "io", "error": {"fenced": 12}}}
        self.assertIsNone(leg.bound_refusal(io_fenced))
        self.assertEqual(leg.claim_verdict(io_fenced), "fenced")


class ToolSurface(unittest.TestCase):
    """The shipped `dcs-plant-ctl` invocations: an applied step is no
    refusal, the tool's own argument boundary names `parse`, and a
    refusal the field carries names `wire`."""

    def test_an_applied_step_is_no_refusal(self):
        self.assertIsNone(
            leg.ctl_refusal_name((0, '{"result": "stepped"}', ""))
        )

    def test_the_argument_boundary_names_parse(self):
        invocation = (
            1, "",
            'invalid step "1e308": expected a finite number no greater '
            "than 1000000\nusage: …",
        )
        self.assertEqual(leg.ctl_refusal_name(invocation), "parse")

    def test_a_server_carried_refusal_names_wire(self):
        invocation = (
            1, "",
            "dcs-plant-ctl: 127.0.0.1:4700: server refused the request: "
            "step dt must be finite, non-negative, and at most 1000000",
        )
        self.assertEqual(leg.ctl_refusal_name(invocation), "wire")

    def test_a_transport_failure_names_other(self):
        invocation = (
            1, "",
            "dcs-plant-ctl: cannot reach the plant server at "
            "127.0.0.1:4700: connection refused",
        )
        self.assertEqual(leg.ctl_refusal_name(invocation), "other")


class Resolution(unittest.TestCase):
    """The driven point resolves off the emitted model and the served
    census — the leg exercises the declared junction, never a
    hard-coded id."""

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

    def test_no_finite_float_resolves_none(self):
        self.assertEqual(
            leg.probe_point(model(), [], set()), (None, False)
        )


class WireSpelling(unittest.TestCase):
    """The finding's vector on the harness client: `1e308` is a finite
    number, so the leg's own request spelling carries it verbatim and
    no raw-line seam stands between the probe and the wire."""

    def test_the_probe_reaches_the_wire_verbatim(self):
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
        answer = leg.simulate.PlantClient.request(
            client, {"op": "step", "dt": leg.HUGE_DT}
        )
        self.assertEqual(
            stream.written, ['{"op": "step", "dt": 1e+308}\n']
        )
        self.assertEqual(answer, {"error": {"kind": "invalid_request"}})


class Inconclusive(unittest.TestCase):
    """The release-precedence classification: a rig predating the
    shared-claim seam or the bounded step contract raises the leg's
    Inconclusive, which main renders as a stable digest line and a zero
    exit — and a doctored case over an inconclusive run still fails,
    carrying the doctored expectation's named evidence."""

    def test_main_reports_the_inconclusive_digest(self):
        rc, out, err = run_main(
            outcome=leg.Inconclusive(
                "the over-bound step was applied — the pinned release "
                "predates the bounded step contract"
            )
        )
        self.assertEqual(rc, 0)
        self.assertIn(
            "step-bound-digest inconclusive — the over-bound step was "
            "applied",
            out,
        )
        self.assertIn("step-bound: inconclusive", err)

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
            "the doctored expectation wanted the dt applied", err
        )


class Classification(unittest.TestCase):
    """main()'s exit classification over the pass result — failures
    stream to stderr prefixed by the stem, a doctored case may never
    exit zero, and a clean pass renders the sha digest line."""

    def test_failures_report_by_name(self):
        rc, out, err = run_main(
            outcome=([], {}, ["the 1e308 step was applied"])
        )
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn(
            "step-bound: the 1e308 step was applied", err
        )

    def test_an_abort_reports_by_name(self):
        rc, out, err = run_main(
            outcome=leg.Abort("the pair never converged")
        )
        self.assertEqual(rc, 1)
        self.assertIn("step-bound: the pair never converged", err)

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
                ["the over-bound step answered refused — the doctored "
                 "expectation wanted the dt applied"],
            ),
        )
        self.assertEqual(rc, 1)

    def test_a_clean_pass_renders_the_digest_line(self):
        evidence = {
            "converged": 4,
            "point": 12,
            "stepped_to": 8,
            "final_tick": 9,
        }
        rc, out, err = run_main(
            outcome=([{"phase": "converge"}], evidence, [])
        )
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"^step-bound-digest [0-9a-f]{64} — ")
        self.assertIn("tracking by tick 4", out)
        self.assertIn("point 12", out)
        self.assertIn("finding's 1e308 step", out)
        self.assertIn("the census unchanged", out)
        self.assertIn("plant to tick 8", out)
        self.assertIn("finite through tick 9", out)


if __name__ == "__main__":
    unittest.main()