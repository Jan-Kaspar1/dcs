"""The plant_loss leg's unit coverage — ci/legs/plant_loss.py is the
reference plant's consumer-boundary mirror of the qa rig's
plant-link-loss scenario (`3500_plant_link_loss`, requirement key
`plant-link-loss`): stopping the deployed pair's plant server leaves the
field owner scanning with degraded served telemetry while the standby
never promotes, and the restarted plant answers third-party mutations
with the named `unclaimed` refusal until the recorded owner's re-attach
re-arms the claim. These tests pin, without launching the pair: the
leg's registration record, the degraded/fail-closed classifications its
window reads through, the staging seams it reuses rather than
reimplements, and the inconclusive / doctored-case classifications
`main` renders.
"""
import contextlib
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
_CI_DIR = _ROOT / "reference-plant" / "ci"
_LEG_PATH = _CI_DIR / "legs" / "plant_loss.py"
_SCENARIO_PATH = _CI_DIR / "scenario.json"
_MODEL_PATH = _ROOT / "reference-plant" / "model" / "plant.json"
_MANIFEST_PATH = _ROOT / "reference-plant" / "deploy" / "manifest.json"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


legs = load(_CI_DIR / "legs.py", "legs")
leg = load(_LEG_PATH, "plant_loss")
model = json.loads(_MODEL_PATH.read_text())


def argv(tamper=None):
    args = [
        "plant_loss.py",
        "--plant-server", "/nonexistent/dcs-plant-server",
        "--controller", "/nonexistent/dcs-controller",
        "--model", "/nonexistent/plant.json",
        "--dynamics", "/nonexistent/dynamics.json",
        "--scenario", str(_SCENARIO_PATH),
        "--manifest", str(_MANIFEST_PATH),
    ]
    if tamper is not None:
        args += ["--tamper", tamper]
    return args


def run_main(tamper=None, outcome=None):
    """`main()` against a stubbed pass — `outcome` the return value
    or the exception plant_loss_pass raises. Returns
    `(rc, stdout, stderr)`."""
    if isinstance(outcome, BaseException):
        stub = mock.Mock(side_effect=outcome)
    else:
        stub = mock.Mock(return_value=outcome)
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", argv(tamper)), \
            mock.patch.object(leg, "plant_loss_pass", stub), \
            contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        rc = leg.main()
    return rc, out.getvalue(), err.getvalue()


class Registration(unittest.TestCase):
    """The leg's `LEG` literal — the pair stage's discovery contract:
    the declared order is free and unique in the directory, the stem
    and the named diagnostics follow the file-name convention, and
    every doctored case carries the evidence the honest run reports."""

    def test_the_leg_registers_in_the_pair_stage(self):
        discovered = legs.discover(str(_CI_DIR / "legs"))
        record = [
            item for item in discovered if item["stem"] == "plant-loss"
        ]
        self.assertEqual(len(record), 1)
        self.assertEqual(record[0]["order"], 890)
        orders = [item["order"] for item in discovered]
        self.assertEqual(len(orders), len(set(orders)))

    def test_the_named_diagnostics_follow_the_stem_convention(self):
        self.assertNotIn("failed", leg.LEG)
        self.assertEqual(leg.LEG["passes"], "plant-loss")

    def test_the_doctored_cases_carry_named_evidence(self):
        tampers = {case["name"]: case for case in leg.LEG["tampers"]}
        self.assertEqual(
            set(tampers),
            {
                "expect-owner-exit",
                "expect-self-promotion",
                "expect-open-window",
                "expect-reset-counters",
                "skip-outage",
            },
        )
        source = _LEG_PATH.read_text()
        for name, case in tampers.items():
            self.assertTrue(case["evidence"], name)
            for evidence in case["evidence"]:
                self.assertIn(evidence, source, name)
            for field in ("passed", "missed"):
                self.assertIsInstance(case[field], str)

    def test_the_contract_declares_the_emitted_diagnostics(self):
        contract = (_ROOT / "docs" / "release-contract.md").read_text()
        for name in (
            "`plant-loss-failed`",
            "`plant-loss-nondeterministic`",
            "`plant-loss-unchecked`",
        ):
            self.assertIn(name, contract)

    def test_the_leg_joins_the_boundary_lints_file_list(self):
        # The pair stage's leg convention is a directory rule, so a new
        # leg registers by file — the lint's `ci/legs/*.py` glob covers
        # it without an edit.
        check = (_CI_DIR / "check.sh").read_text()
        self.assertIn("ci/legs/*.py", check)


class Classifications(unittest.TestCase):
    """The verdict words the leg's outage and fail-closed windows read
    through — the degraded serve, the tracking posture, and the model's
    channel-backed probe point."""

    def test_the_degraded_serve_is_the_drivers_own_predicate(self):
        health = {
            "consecutive_failures": 2,
            "last_error": "connection refused",
            "failed_reads": 3,
            "failed_writes": 1,
        }
        driver = {"link": "disconnected", "last_error": "refused"}
        self.assertTrue(leg.degraded(health, driver))
        self.assertFalse(leg.degraded(health, {"link": "connected"}))
        self.assertFalse(leg.degraded({"consecutive_failures": 0}, driver))

    def test_the_driver_section_reads_from_the_served_io_health(self):
        driver = {"link": "connected", "last_error": None}
        self.assertEqual(
            leg.driver_health({"io_health": {"driver": driver}}), driver
        )
        self.assertIsNone(leg.driver_health({"io_health": {}}))
        self.assertIsNone(leg.driver_health({"io_health": {"driver": 3}}))
        self.assertIsNone(leg.driver_health(None))

    def test_only_a_tracking_standby_reads_tracking(self):
        self.assertTrue(
            leg.tracking({"role": "standby", "sync": {"tracking": {}}})
        )
        self.assertFalse(
            leg.tracking({"role": "standby", "sync": "unsynchronized"})
        )
        self.assertFalse(leg.tracking({"role": "active"}))

    def test_the_probe_point_is_a_channel_backed_field_input(self):
        point = leg._probe_point(str(_MODEL_PATH))
        declared = {
            entry["id"]: entry for entry in model["io_points"]
        }
        self.assertIn(point, declared)
        self.assertTrue(declared[point].get("channel"))


class Staging(unittest.TestCase):
    """The seams the leg drives rather than reimplements — the pair
    harness's launch/tick, the remote-driver recovery leg's degraded
    read and its plant-respawn lever, and the failover leg's probe and
    owner-token reads."""

    def test_the_leg_reuses_the_pair_and_driver_recovery_staging(self):
        for helper in (
            leg.pair.launch_pair,
            leg.pair.tick,
            leg.pair.stop,
            leg.pair.PairRig,
            leg.driver_recovery.driver_health,
            leg.driver_recovery.degraded,
            leg.failover.probe_kind,
            leg.failover.owner_token,
            leg.stranded_rejoin.sync_kind,
        ):
            self.assertTrue(callable(helper), helper)
        self.assertIn("cmd", leg.failover.SIGNALS)

    def test_the_respawn_lever_is_the_drivers_own(self):
        with mock.patch.object(
            leg.driver_recovery, "respawn_plant"
        ) as stub:
            leg.respawn_plant("args", "rig")
        stub.assert_called_once_with("args", "rig")

    def test_the_degraded_predicate_is_reused_not_copied(self):
        cases = [
            ({}, {}),
            ({"consecutive_failures": 2, "last_error": "x"},
             {"link": "disconnected", "last_error": "x"}),
            ({"consecutive_failures": 2, "last_error": "x"},
             {"link": "connected"}),
            ({"last_error": "x"}, {"link": "disconnected"}),
        ]
        for health, driver in cases:
            self.assertEqual(
                leg.degraded(health, driver),
                leg.driver_recovery.degraded(health, driver),
                (health, driver),
            )

    def test_the_driven_scan_bounds_are_positive(self):
        for bound in (
            leg.OUTAGE_SCANS,
            leg.REATTACH_SCANS,
            leg.FAIL_CLOSED_PROBES,
            leg.SETTLE_TICKS,
        ):
            self.assertGreater(bound, 0)

    def test_the_fencing_vocabulary_is_the_fault_contracts_own(self):
        # The window's probes must never be `granted` — only the two
        # fail-closed shapes, and the unclaimed one is what the restarted
        # plant owes before the owner re-arms.
        self.assertEqual(
            leg.failover.probe_kind({"result": "done"}), "granted"
        )
        unclaimed = {
            "result": "error",
            "error": {"kind": "unclaimed", "detail": "no claim stands"},
        }
        self.assertEqual(leg.failover.probe_kind(unclaimed), "unclaimed")


class Inconclusive(unittest.TestCase):
    """The release-precedence classification: a pinned release
    predating the field-loss contract raises the leg's Inconclusive,
    which main renders as a stable digest line and a zero exit — and a
    doctored case over an inconclusive run still fails, since it can
    name no evidence."""

    def test_main_reports_the_inconclusive_digest(self):
        rc, out, err = run_main(
            outcome=leg.Inconclusive(
                "the launched active recorded no claim line — the "
                "pinned release claims only on promotion"
            )
        )
        self.assertEqual(rc, 0)
        self.assertIn("plant-loss-digest inconclusive", out)
        self.assertIn("plant-loss: inconclusive —", err)

    def test_two_inconclusive_passes_render_identically(self):
        first = run_main(outcome=leg.Inconclusive("pre-contract"))
        second = run_main(outcome=leg.Inconclusive("pre-contract"))
        self.assertEqual(first[0], 0)
        self.assertEqual(first[1], second[1])

    def test_a_doctored_case_over_an_inconclusive_run_fails(self):
        rc, out, err = run_main(
            tamper="expect-open-window",
            outcome=leg.Inconclusive("pre-contract"),
        )
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("offers it no evidence", err)


class Classification(unittest.TestCase):
    """main()'s exit classification over the pass result — failures
    stream to stderr prefixed by the stem, a doctored case may never
    exit zero, and a clean pass renders the sha digest line."""

    def test_failures_report_by_name(self):
        rc, out, err = run_main(
            outcome=([], {}, ["the field owner stayed active"])
        )
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("plant-loss: the field owner stayed active", err)

    def test_an_abort_reports_by_name(self):
        rc, out, err = run_main(
            outcome=leg.Abort("the manifest declares no standby pair")
        )
        self.assertEqual(rc, 1)
        self.assertIn("plant-loss: the manifest declares no standby pair",
                      err)

    def test_a_doctored_case_never_passes_silently(self):
        for tamper in (
            "expect-owner-exit",
            "expect-self-promotion",
            "expect-open-window",
            "expect-reset-counters",
            "skip-outage",
        ):
            rc, _, err = run_main(tamper=tamper, outcome=([], {}, []))
            self.assertEqual(rc, 1, tamper)
            self.assertIn("passed silently", err, tamper)

    def test_a_doctored_case_carrying_failures_still_fails(self):
        rc, _, err = run_main(
            tamper="expect-open-window",
            outcome=([], {}, ["named evidence"]),
        )
        self.assertEqual(rc, 1)
        self.assertIn("named evidence", err)

    def test_a_clean_pass_renders_the_digest_line(self):
        evidence = {
            "converged": 4,
            "outage_at": 12,
            "fail_closed": ["unclaimed", "unclaimed"],
            "rearmed_at": 9,
            "final_tick": 20,
        }
        rc, out, err = run_main(
            outcome=([{"phase": "converge"}], evidence, [])
        )
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"^plant-loss-digest [0-9a-f]{64} — ")
        self.assertIn("tracking by tick 4", out)
        self.assertIn("counted 12 failed reads", out)
        self.assertIn("re-armed it at tick 9", out)
        self.assertIn("run continued to tick 20", out)
        self.assertEqual(err, "")


class DoctoredEvidence(unittest.TestCase):
    """Each doctored case's stable evidence prefix — the string the
    pair stage's negative self-test looks for in the tampered pass's
    output."""

    def test_every_prefix_is_present_in_the_source(self):
        source = _LEG_PATH.read_text()
        for prefix in (
            leg.TAMPER_OWNER_EXIT,
            leg.TAMPER_OPEN_WINDOW,
            leg.TAMPER_RESET_COUNTERS,
        ):
            self.assertIn(prefix, source)

    def test_the_prefixes_are_distinct(self):
        self.assertEqual(
            len({leg.TAMPER_OWNER_EXIT, leg.TAMPER_OPEN_WINDOW,
                 leg.TAMPER_RESET_COUNTERS}),
            3,
        )


if __name__ == "__main__":
    unittest.main()