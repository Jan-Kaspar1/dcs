"""The release-orphan-window leg's decision seams, unit-tested:
`release_orphan_window_pass` is the consumer-boundary mirror of the
qa rig's bounded interlock-release orphan-window leg (#1179's rig
leg, #828's fix), so what these tests pin is the machinery its verdict
rests on — the model's own signal resolution and its writable operator
seam, the served journal readers that name the ownerless transition
and the demote's own walk, the sync vocabulary the bound reads, and
the `Inconclusive` classification that keeps a pinned release predating
the contract an inconclusive reading rather than a product failure.

The tests also pin the leg's own audit: the doctored cases' stable
evidence phrases are substrings the leg renders (so `ci/legs.py`'s
negative self-check can match them), the leg registers a free order
beside the directory's other legs, its actor is its own attribution,
the release contract declares the three diagnostics it reports, the
predating digest line carries only the stable reason both passes share
while a doctored case still fails, and the leg names no platform
checkout path.
"""
import argparse
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_CI_DIR = _ROOT / "reference-plant" / "ci"
_PATH = _CI_DIR / "legs" / "release_orphan_window.py"
_spec = importlib.util.spec_from_file_location("release_orphan_window",
                                               _PATH)
leg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(leg)

_MODEL = json.loads(
    (_CI_DIR.parent / "model" / "plant.json").read_text()
)

_SYNC_ROLES = (
    ({"role": "standby", "sync": {"tracking": {"aligned": 9}}}, "tracking"),
    ({"role": "standby", "sync": {"orphaned": {"aligned": 9}}}, "orphaned"),
    ({"role": "standby", "sync": {"diverged": {"aligned": 9}}}, "diverged"),
    ({"role": "standby", "sync": "unsynchronized"}, "unsynchronized"),
    ({"role": "standby", "sync": "degraded"}, "degraded"),
)


class ResolvedSeam(unittest.TestCase):
    """The model resolution the leg drives: every point comes from the
    emitted model's own signal index by name, never a hard-coded id,
    and the four operator points must be declared writable — the leg's
    receipted hand run has no other path to the pumps."""

    def test_every_driven_point_resolves_by_name(self):
        points = leg.signal_points(_MODEL)
        self.assertIsNotNone(points)
        self.assertEqual(14, points["power_fail"])
        self.assertEqual(206, points["power_ok"])
        self.assertEqual(100, points["cmd_1"])
        self.assertEqual(101, points["cmd_2"])
        self.assertEqual(300, points["mode_1"])
        self.assertEqual(301, points["hand_1"])
        self.assertEqual(332, points["mode_2"])
        self.assertEqual(333, points["hand_2"])

    def test_a_model_without_the_protection_seam_resolves_nothing(self):
        model = dict(_MODEL, signals=[
            signal for signal in _MODEL["signals"]
            if signal["name"] != "power-fail"])
        self.assertIsNone(leg.signal_points(model))

    def test_a_model_with_read_only_operator_points_resolves_nothing(self):
        model = json.loads(json.dumps(_MODEL))
        for point in model["io_points"]:
            if point["id"] in (300, 301, 332, 333):
                point["writable"] = False
        self.assertIsNone(leg.signal_points(model))


class ServedVocabulary(unittest.TestCase):
    """The served shapes the bound reads: the sync vocabulary's one
    word — the promote-blocking `diverged` among it — the field reads'
    energization, and the durable records naming the ownerless line."""

    def test_each_sync_variant_reads_as_one_word(self):
        for report, wanted in _SYNC_ROLES:
            self.assertEqual(wanted, leg.sync_word(report))

    def test_an_absent_or_unread_sync_reads_as_none(self):
        self.assertIsNone(leg.sync_word(None))
        self.assertIsNone(leg.sync_word({}))
        self.assertIsNone(leg.sync_word({"role": "active"}))

    def test_only_an_asserted_bool_reads_energized(self):
        self.assertTrue(leg.energized({"bool": True}))
        self.assertFalse(leg.energized({"bool": False}))
        self.assertFalse(leg.energized({"float": 1.5}))
        self.assertFalse(leg.energized(None))

    def test_the_orphan_transition_is_read_off_the_journal(self):
        entries = [
            {"seq": 4, "event": {"role_changed": {
                "from": "active", "to": "demoting", "origin": "request"}}},
            {"seq": 5, "event": {"field_orphaned": {"aligned": 18}}},
            {"seq": 6, "event": {"field_orphaned": "not-a-record"}},
        ]
        self.assertEqual([{"aligned": 18}], leg.journal_orphans(entries))
        self.assertEqual([("active", "demoting", "request")],
                         leg.journal_walk(entries))

    def test_an_absent_or_unread_journal_reads_as_no_evidence(self):
        self.assertEqual([], leg.journal_orphans(None))
        self.assertEqual([], leg.journal_orphans([{"seq": 1, "event": {}}]))
        self.assertEqual([], leg.journal_walk([]))


class Declaration(unittest.TestCase):
    """The run's own declarations: the bounds are driven-scan counts,
    the doctored cases carry stable evidence, and the actor is the
    leg's own attribution."""

    def test_the_bound_is_declared_in_driven_scans(self):
        # The declared bound must span the window it observes plus the
        # promoted successor's first field-owning scans — the release
        # the contract says must never wait for an operator.
        self.assertGreater(leg.RELEASE_SCANS, leg.WINDOW_ROUNDS)
        self.assertGreaterEqual(leg.RELEASE_SCANS, 3 * leg.WINDOW_ROUNDS)
        self.assertGreater(leg.HAND_SCANS, leg.WINDOW_ROUNDS)
        self.assertGreater(leg.SETTLE_TICKS, 0)

    def test_both_halves_of_the_contract_are_staged(self):
        self.assertEqual({"expect-flushed", "skip-promote"},
                         {tamper["name"]
                          for tamper in leg.LEG["tampers"]})

    def test_the_declared_evidence_is_a_phrase_the_leg_renders(self):
        rendered = leg.TAMPER_EXPECT_FLUSHED + leg.TAMPER_SKIP_PROMOTE
        for tamper in leg.LEG["tampers"]:
            for phrase in tamper["evidence"]:
                self.assertIn(phrase, rendered, tamper["name"])

    def test_the_predating_reason_is_one_stable_string(self):
        self.assertTrue(leg.PRE_CONTRACT_REASON.startswith(
            "the pinned release predates"))

    def test_a_model_without_the_seam_reports_the_predating_verdict(self):
        # A release whose emitted model never composed the protection
        # wiring cannot present the contract: the leg must read that as
        # the predating verdict, not as the product failure the
        # contract's own diagnostic names.
        scratch = self.enterContext(tempfile.TemporaryDirectory())
        emitted = Path(scratch) / "plant.json"
        emitted.write_text(json.dumps(dict(
            _MODEL,
            signals=[signal for signal in _MODEL["signals"]
                     if signal["name"] != "power-fail"])))
        args = argparse.Namespace(
            manifest=str(_CI_DIR.parent / "deploy" / "manifest.json"),
            model=str(emitted))
        with unittest.mock.patch.object(
                leg, "signal_points", return_value=None):
            with self.assertRaises(leg.Inconclusive) as raised:
                leg.release_orphan_window_pass(args, None)
        self.assertEqual(leg.SEAM_ABSENT_REASON, raised.exception.args[0])
        self.assertTrue(leg.SEAM_ABSENT_REASON.startswith(
            "the emitted model"))

    def test_the_actor_is_the_leg_own_attribution(self):
        self.assertTrue(leg.ACTOR.startswith("ci-"))


class InconclusiveReport(unittest.TestCase):
    """`main`'s report: the digest line carries only the stable reason,
    so two identical passes agree, and a doctored case still fails —
    an inconclusive run offers it no evidence."""

    def run_main(self, tamper):
        out, err = io.StringIO(), io.StringIO()
        original = leg.release_orphan_window_pass
        leg.release_orphan_window_pass = lambda args, chosen: (
            _ for _ in ()).throw(leg.Inconclusive(
                leg.PRE_CONTRACT_REASON,
                "the ownerless window left the staged-versus-field "
                "divergence and the promote answered not_converged"))
        argv = [
            "release_orphan_window.py",
            "--plant-server", "dcs-plant-server",
            "--controller", "dcs-controller",
            "--model", str(_CI_DIR.parent / "model" / "plant.json"),
            "--dynamics", str(_CI_DIR.parent / "model" / "dynamics.json"),
            "--scenario", str(_CI_DIR / "scenario.json"),
            "--manifest", str(_CI_DIR.parent / "deploy" / "manifest.json"),
        ]
        if tamper is not None:
            argv += ["--tamper", tamper]
        saved = sys.argv
        sys.argv = argv
        try:
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                status = leg.main()
        finally:
            sys.argv = saved
            leg.release_orphan_window_pass = original
        return status, out.getvalue(), err.getvalue()

    def test_the_digest_line_carries_only_the_stable_reason(self):
        status, out, err = self.run_main(None)
        self.assertEqual(0, status)
        self.assertEqual(
            "release-orphan-window-digest inconclusive — "
            + leg.PRE_CONTRACT_REASON + "\n", out)
        self.assertIn("not_converged", err)
        self.assertNotIn("owner token", out)

    def test_both_passes_share_the_one_reason(self):
        reasons = {self.run_main(None)[1] for _ in range(2)}
        self.assertEqual(1, len(reasons))

    def test_a_doctored_case_fails_on_an_inconclusive_run(self):
        for tamper in ("expect-flushed", "skip-promote"):
            status, _out, _err = self.run_main(tamper)
            self.assertEqual(1, status, tamper)


class Registration(unittest.TestCase):
    def test_the_leg_registers_a_free_order_with_a_doctored_case(self):
        self.assertEqual(205, leg.LEG["order"])
        self.assertEqual("release-orphan-window-leg", leg.LEG["passes"])
        self.assertEqual("release-orphan-window-failed",
                         leg.LEG["failed"])
        self.assertTrue(leg.LEG["tampers"])

    def test_the_leg_validates_beside_the_directorys_others(self):
        driver_spec = importlib.util.spec_from_file_location(
            "legs", _CI_DIR / "legs.py")
        driver = importlib.util.module_from_spec(driver_spec)
        driver_spec.loader.exec_module(driver)
        discovered = driver.discover(str(_CI_DIR / "legs"))
        orders = [found["order"] for found in discovered]
        self.assertEqual(len(orders), len(set(orders)),
                         "two legs share an order")
        stems = [found["stem"] for found in discovered]
        self.assertIn("release-orphan-window", stems)

    def test_the_order_lands_inside_the_demote_cluster(self):
        driver_spec = importlib.util.spec_from_file_location(
            "legs", _CI_DIR / "legs.py")
        driver = importlib.util.module_from_spec(driver_spec)
        driver_spec.loader.exec_module(driver)
        discovered = driver.discover(str(_CI_DIR / "legs"))
        order = [Path(found["file"]).stem for found in discovered]
        self.assertLess(order.index("demote_reconvergence"),
                        order.index("release_orphan_window"))
        self.assertLess(order.index("release_orphan_window"),
                        order.index("managed_lifecycle"))

    def test_the_contract_declares_the_emitted_diagnostics(self):
        contract = (_ROOT / "docs" / "release-contract.md").read_text()
        for name in (
            "`release-orphan-window-failed`",
            "`release-orphan-window-nondeterministic`",
            "`release-orphan-window-unchecked`",
        ):
            self.assertIn(name, contract)

    def test_the_leg_references_no_platform_checkout_path(self):
        source = _PATH.read_text()
        for leak in ("crates/", "../", "file://", "/home/", "target/debug"):
            self.assertNotIn(leak, source, f"the leg names {leak}")


if __name__ == "__main__":
    unittest.main()