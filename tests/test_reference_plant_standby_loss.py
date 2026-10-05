"""The standby_loss leg's unit coverage — ci/legs/standby_loss.py is
the reference plant's consumer-boundary mirror of the qa rig's
standby-loss scenario (`1700_standby_loss`, requirement key
`standby-loss`): a tracking standby settles nothing (the named
`not_active` admission refusal with no field effect and no journaled
command on the field owner), the controller of record reacts to
nothing while its standby is down (the served tick advancing, a
receipted command settling, its role and both journals undisturbed),
and the returning peer reconverges to `tracking` inside the leg's
declared window while a promote fired before its first transfer
completes answers the named `not_converged` refusal. These tests pin,
without launching the pair: the leg's registration record, the
verdict and role classifications its legs read through, the staging
seams it reuses rather than reimplements, and the inconclusive /
doctored-case classifications `main` renders.
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
_LEG_PATH = _CI_DIR / "legs" / "standby_loss.py"
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
leg = load(_LEG_PATH, "standby_loss")
model = json.loads(_MODEL_PATH.read_text())


def argv(tamper=None):
    args = [
        "standby_loss.py",
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
    or the exception standby_loss_pass raises. Returns
    `(rc, stdout, stderr)`."""
    if isinstance(outcome, BaseException):
        stub = mock.Mock(side_effect=outcome)
    else:
        stub = mock.Mock(return_value=outcome)
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", argv(tamper)), \
            mock.patch.object(leg, "standby_loss_pass", stub), \
            contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        rc = leg.main()
    return rc, out.getvalue(), err.getvalue()


def entry(seq, tick, event):
    return {"seq": seq, "tick": tick, "event": event}


class Registration(unittest.TestCase):
    """The leg's `LEG` literal — the pair stage's discovery contract:
    the declared order is free and unique in the directory, the stem
    and the named diagnostics follow the file-name convention, and
    every doctored case carries the evidence the honest run reports."""

    def test_the_leg_registers_in_the_pair_stage(self):
        discovered = legs.discover(str(_CI_DIR / "legs"))
        record = [
            item for item in discovered if item["stem"] == "standby-loss"
        ]
        self.assertEqual(len(record), 1)
        self.assertEqual(record[0]["order"], 880)
        orders = [item["order"] for item in discovered]
        self.assertEqual(len(orders), len(set(orders)))

    def test_the_named_diagnostics_follow_the_stem_convention(self):
        self.assertNotIn("failed", leg.LEG)
        self.assertEqual(leg.LEG["passes"], "standby-loss")

    def test_the_doctored_cases_carry_named_evidence(self):
        tampers = {case["name"]: case for case in leg.LEG["tampers"]}
        self.assertEqual(
            set(tampers),
            {
                "expect-applied",
                "expect-peer-reaction",
                "expect-open-gate",
                "skip-restart",
                "skip-down-window",
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
            "`standby-loss-failed`",
            "`standby-loss-nondeterministic`",
            "`standby-loss-unchecked`",
        ):
            self.assertIn(name, contract)


class Classifications(unittest.TestCase):
    """The verdict words the leg's legs read through — the tracking and
    unconverged role shapes, the settled-receipt read, and the model's
    writable-boolean point."""

    def test_a_tracking_standby_reads_tracking(self):
        self.assertTrue(
            leg.tracking({"role": "standby", "sync": {"tracking": {}}})
        )
        self.assertFalse(
            leg.tracking({"role": "standby", "sync": "unsynchronized"})
        )
        self.assertFalse(leg.tracking({"role": "active"}))

    def test_the_sync_variant_reads_from_either_wire_shape(self):
        self.assertEqual(
            leg.sync_kind({"sync": {"degraded": {"detail": "x"}}}),
            "degraded",
        )
        self.assertEqual(leg.sync_kind({"sync": "unsynchronized"}),
                         "unsynchronized")
        self.assertIsNone(leg.sync_kind({"role": "active"}))

    def test_only_an_applied_receipt_counts_as_settled(self):
        command = {"write_value": {"point": 240}}
        receipts = [
            {"command": command, "outcome": {"accepted": {"apply_tick": 4}}},
            {"command": command, "outcome": {"applied": {"tick": 5}}},
        ]
        self.assertEqual(len(leg.settled(receipts, command)), 1)
        self.assertEqual(leg.settled(receipts[:1], command), [])

    def test_the_journal_audit_reads_the_command_settled_records(self):
        refusal = {
            "command": {"write_value": {"point": 240}},
            "outcome": {"rejected": {"reason": {
                "not_active": {"point": 240}}}},
        }
        receipts = leg.command_receipts(
            [
                entry(1, 3, {"command_settled": {"receipt": refusal}}),
                entry(2, 4, {"role_changed": {"from": "active",
                                              "to": "demoting"}}),
            ],
            {"write_value": {"point": 240}},
        )
        self.assertEqual(len(receipts), 1)

    def test_the_probe_point_is_the_lowest_writable_boolean(self):
        point = leg.writable_point(model)
        declared = {
            entry["id"]: entry for entry in model["io_points"]
        }
        self.assertIn(point, declared)
        self.assertTrue(declared[point]["writable"])
        self.assertEqual(declared[point]["value_type"], "bool")
        self.assertIsNone(leg.writable_point({"io_points": []}))

    def test_the_durable_role_audit_reads_the_journal_file(self):
        with mock.patch.object(
            leg.pair,
            "journal_records",
            return_value=[
                ("boundary", {"run": 1, "tick": 0}),
                ("entry", entry(1, 3, {"point_changed": {"point": 5}})),
                ("entry", entry(2, 4, {"role_changed": {
                    "from": "active", "to": "demoting"}})),
                ("entry", entry(3, 5, {"run_boundary": {"run": 2}})),
                ("boundary", {"run": 2, "tick": 4}),
            ],
        ):
            records, boundaries = leg._role_entries("ignored.jsonl")
        # A point observation is not a role record; the role walk and the
        # run-boundary marker both are, so the audit's floors move on
        # exactly the records the non-interference leg reads.
        self.assertEqual(len(records), 2)
        self.assertEqual(len(boundaries), 2)


class Staging(unittest.TestCase):
    """The seams the leg drives rather than reimplements — the pair
    harness's launch/switch/tick/relauch and the refusal leg's field
    census and journal projections."""

    def test_the_leg_reuses_the_pair_and_refusal_staging(self):
        for helper in (
            leg.pair.launch_pair,
            leg.pair.tick,
            leg.pair.PairRig,
            leg.pair.stop,
            leg.pair.spawn_peer,
            leg.pair.declared_command,
            leg.refusal.field_out_samples,
            leg.refusal.field_mismatches,
            leg.refusal.command_receipts,
        ):
            self.assertTrue(callable(helper))
        self.assertEqual(leg.refusal.__name__, "refusal")
        self.assertEqual(leg.pair.__name__, "pair")

    def test_the_driven_scan_bounds_are_positive(self):
        for bound in (
            leg.SETTLE_SCANS,
            leg.DOWN_WINDOW_SCANS,
            leg.RECONVERGE_SCANS,
            leg.SETTLE_TICKS,
        ):
            self.assertGreater(bound, 0)


class Inconclusive(unittest.TestCase):
    """The release-precedence classification: a pinned release
    predating the contract raises the leg's Inconclusive, which main
    renders as a stable digest line and a zero exit — and a doctored
    case over an inconclusive run still fails, since it can name no
    evidence."""

    def test_main_reports_the_inconclusive_digest(self):
        rc, out, err = run_main(
            outcome=leg.Inconclusive(
                "the manifest's pair declares no journal files — the "
                "durable half of the outage's audit is absent"
            )
        )
        self.assertEqual(rc, 0)
        self.assertIn("standby-loss-digest inconclusive", out)
        self.assertIn("standby-loss: inconclusive —", err)

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
        self.assertEqual(out, "")
        self.assertIn("offers it no evidence", err)


class Classification(unittest.TestCase):
    """main()'s exit classification over the pass result — failures
    stream to stderr prefixed by the stem, a doctored case may never
    exit zero, and a clean pass renders the sha digest line."""

    def test_failures_report_by_name(self):
        rc, out, err = run_main(
            outcome=([], {}, ["the field owner left active"])
        )
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("standby-loss: the field owner left active", err)

    def test_an_abort_reports_by_name(self):
        rc, out, err = run_main(
            outcome=leg.Abort("the manifest declares no standby pair")
        )
        self.assertEqual(rc, 1)
        self.assertIn("standby-loss: the manifest declares no standby pair",
                      err)

    def test_a_doctored_case_never_passes_silently(self):
        for tamper in (
            "expect-applied",
            "expect-peer-reaction",
            "expect-open-gate",
            "skip-restart",
            "skip-down-window",
        ):
            rc, _, err = run_main(tamper=tamper, outcome=([], {}, []))
            self.assertEqual(rc, 1, tamper)
            self.assertIn("passed silently", err, tamper)

    def test_a_doctored_case_carrying_failures_still_fails(self):
        rc, _, err = run_main(
            tamper="expect-applied",
            outcome=([], {}, ["named evidence"]),
        )
        self.assertEqual(rc, 1)
        self.assertIn("named evidence", err)

    def test_a_clean_pass_renders_the_digest_line(self):
        evidence = {
            "converged": 4,
            "refused_at": 240,
            "down_ticks": 9,
            "reconverged": 12,
            "final_tick": 20,
        }
        rc, out, err = run_main(
            outcome=([{"phase": "converge"}], evidence, [])
        )
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"^standby-loss-digest [0-9a-f]{64} — ")
        self.assertIn("tracking by tick 4", out)
        self.assertIn("refused not_active at tick 240", out)
        self.assertIn("run continued to tick 20", out)
        self.assertEqual(err, "")


class DoctoredEvidence(unittest.TestCase):
    """Each doctored case's stable evidence prefix — the string the
    pair stage's negative self-test looks for in the tampered pass's
    output."""

    def test_every_prefix_is_present_in_the_source(self):
        source = _LEG_PATH.read_text()
        for prefix in (
            leg.TAMPER_APPLIED,
            leg.TAMPER_PEER_REACTION,
            leg.TAMPER_OPEN_GATE,
        ):
            self.assertIn(prefix, source)

    def test_the_prefixes_are_distinct(self):
        self.assertEqual(
            len({leg.TAMPER_APPLIED, leg.TAMPER_PEER_REACTION,
                 leg.TAMPER_OPEN_GATE}),
            3,
        )


if __name__ == "__main__":
    unittest.main()