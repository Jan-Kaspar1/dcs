"""The orphan_episode_bound leg's unit coverage — ci/legs/
orphan_episode_bound.py is the reference plant's consumer-boundary
mirror of the rig's orphan-episode-bound exercise (the #1041
contract): with the manifest-declared pair converged and armed at the
declared `failover_budget`, the demoted pair's island pins the
declared standby on an advancing-but-ownerless source — one journaled
`field_orphaned` for the whole episode, the `orphaned` verdict riding
out the evidence-free pull misses, those misses still counting toward
the armed budget — and a genuinely new episode journals its own entry
before the launch roles restore. These tests pin, without launching
the pair: the leg's registration record, the report and journal
projections its audit reads, the window's row normalization the
verdict and miss clauses replay, and the inconclusive / doctored-case
classifications main renders."""
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
_CI_DIR = _ROOT / "reference-plant" / "ci"
_LEG_PATH = _CI_DIR / "legs" / "orphan_episode_bound.py"
_SCENARIO_PATH = _CI_DIR / "scenario.json"
_MANIFEST_PATH = _ROOT / "reference-plant" / "deploy" / "manifest.json"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


legs = load(_CI_DIR / "legs.py", "legs")
leg = load(_LEG_PATH, "orphan_episode_bound")


def argv(tamper=None):
    args = [
        "orphan_episode_bound.py",
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
    or the exception orphan_episode_bound_pass raises. Returns
    `(rc, stdout, stderr)`."""
    if isinstance(outcome, BaseException):
        stub = mock.Mock(side_effect=outcome)
    else:
        stub = mock.Mock(return_value=outcome)
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", argv(tamper)), \
            mock.patch.object(leg, "orphan_episode_bound_pass", stub), \
            contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        rc = leg.main()
    return rc, out.getvalue(), err.getvalue()


def orphan(seq, tick, aligned):
    return {"seq": seq, "tick": tick,
            "event": {"field_orphaned": {"aligned": aligned}}}


class Registration(unittest.TestCase):
    """The leg's `LEG` literal — the pair stage's discovery contract:
    a unique declared order, the stem-derived `orphan-episode-bound-*`
    diagnostic family, and the doctored case naming the evidence the
    honest run reports."""

    def test_the_leg_registers_in_the_pair_stage(self):
        discovered = {
            Path(record["file"]).name: record
            for record in legs.discover(str(_CI_DIR / "legs"))
        }
        record = discovered["orphan_episode_bound.py"]
        self.assertEqual(record["stem"], "orphan-episode-bound")
        self.assertEqual(record["order"], 880)
        orders = [
            item["order"]
            for item in legs.discover(str(_CI_DIR / "legs"))
        ]
        self.assertEqual(len(orders), len(set(orders)))

    def test_the_diagnostics_follow_the_file_stem(self):
        # No `failed` override: the stage forms
        # orphan-episode-bound-failed on the file stem, as the issue
        # names it.
        self.assertNotIn("failed", leg.LEG)
        self.assertEqual(leg.LEG["passes"], "orphan-episode-bound-leg")
        self.assertEqual(
            legs.leg_stem("orphan_episode_bound.py"),
            "orphan-episode-bound",
        )

    def test_the_doctored_case_carries_named_evidence(self):
        tampers = {entry["name"]: entry for entry in leg.LEG["tampers"]}
        self.assertEqual(set(tampers), {"expect-flood"})
        source = _LEG_PATH.read_text()
        for name, record in tampers.items():
            self.assertTrue(record["evidence"], name)
            for evidence in record["evidence"]:
                self.assertIn(evidence, source, name)
            for field in ("passed", "missed"):
                self.assertIsInstance(record[field], str)


class Projections(unittest.TestCase):
    """The report and journal projections the episode audit reads:
    the orphan posture, the armed heartbeat's served evidence, and
    the `field_orphaned` records off both the served tail and the
    declared durable file."""

    def test_the_orphan_posture_is_the_pinned_standby(self):
        self.assertTrue(
            leg.orphan_report(
                {"role": "standby", "sync": {"orphaned": {"aligned": 5}}}
            )
        )
        self.assertFalse(
            leg.orphan_report(
                {"role": "standby", "sync": {"tracking": {"aligned": 5}}}
            )
        )
        self.assertFalse(
            leg.orphan_report(
                {"role": "standby", "sync": {"degraded": {"detail": "x"}}}
            )
        )
        # The field owner reports no sync section at all.
        self.assertFalse(leg.orphan_report({"role": "active"}))

    def test_the_armed_evidence_projects_converged_misses_budget(self):
        self.assertEqual(
            leg.failover_evidence(
                {"failover": {"converged": True, "misses": 2, "budget": 3}}
            ),
            (True, 2, 3),
        )
        self.assertIsNone(leg.failover_evidence({"role": "standby"}))

    def test_orphan_entries_project_in_seq_order(self):
        entries = [
            orphan(3, 7, 5),
            {"seq": 4, "tick": 8, "event": {"role_changed": {
                "from": "standby", "to": "standby"}}},
            orphan(5, 9, 7),
        ]
        self.assertEqual(
            [seq for seq, _record in leg.orphan_entries(entries)], [3, 5]
        )

    def test_the_durable_count_reads_the_journal_file(self):
        records = [
            {"run_boundary": {"run": 1, "tick": 0}},
            {"entry": orphan(1, 2, 5)},
            {"entry": orphan(2, 5, 8)},
        ]
        with tempfile.NamedTemporaryFile(
            "w", suffix=".jsonl", delete=False
        ) as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
            path = handle.name
        self.assertEqual(leg.durable_orphans(path), 2)

    def test_episode_counts_read_the_served_tail_before_the_file(self):
        # The served journal serves bare entry records; the durable
        # file wraps each in its own line record. The served read comes
        # first in `episode_counts` — it waits the durable sink's drain
        # out — so the file is caught up through the same tail and the
        # two halves agree on a live run.
        entries = [orphan(1, 2, 5), orphan(2, 5, 8), orphan(3, 7, 11)]
        with tempfile.NamedTemporaryFile(
            "w", suffix=".jsonl", delete=False
        ) as handle:
            for entry in entries:
                handle.write(json.dumps({"entry": entry}) + "\n")
            path = handle.name
        self.assertEqual(
            leg.episode_counts(
                entries[1:], path, {"served": 1, "durable": 1}
            ),
            {"served": 1, "durable": 2},
        )


class Arming(unittest.TestCase):
    """The pinned probe's arming — a leg-spawned peer, sized past the
    whole staged window so the promotion boundary never closes it,
    while the manifest's declared `failover_budget` stays the
    deployment's own."""

    def test_the_probe_budget_clears_every_staged_pull(self):
        armed = leg.probe_budget(3)
        staged = (
            leg.PIN_SCANS
            + leg.HOLD_ROUNDS * (leg.FROZEN_SCANS + leg.LIVE_SCANS)
        )
        self.assertGreater(armed, 3)
        self.assertGreaterEqual(armed - leg.PROBE_MISS_HEADROOM, staged)

    def test_a_wider_declared_budget_widens_the_probe_with_it(self):
        self.assertEqual(
            leg.probe_budget(7) - leg.probe_budget(3), 4)


class Window(unittest.TestCase):
    """The window's row normalization the verdict and miss clauses
    replay: one row per observation, carrying the phase it was read
    in, the reported role, the sync variant, and the armed evidence
    as a stable list so the digest stays port- and value-stable."""

    def test_a_row_normalizes_the_served_report(self):
        row = leg.observation(
            1, "frozen",
            {"role": "standby", "sync": {"orphaned": {"aligned": 9}},
             "failover": {"converged": True, "misses": 2, "budget": 3}},
        )
        self.assertEqual(
            row,
            {"round": 1, "phase": "frozen", "role": "standby",
             "sync": "orphaned", "failover": [True, 2, 3]},
        )

    def test_a_report_without_a_sync_section_reads_missing(self):
        row = leg.observation(0, "live", {"role": "active"})
        self.assertEqual(row["sync"], "missing")
        self.assertEqual(row["failover"], [])

    def test_a_frozen_process_is_required_and_thawed_by_signal(self):
        process = mock.Mock()
        process.poll.return_value = None
        leg.freeze(process)
        process.send_signal.assert_called_once()
        signal_name = process.send_signal.call_args[0][0].name
        self.assertEqual(signal_name, "SIGSTOP")
        leg.thaw(process)
        self.assertEqual(
            process.send_signal.call_args[0][0].name, "SIGCONT")

    def test_a_dead_source_refuses_the_freeze(self):
        process = mock.Mock()
        process.poll.return_value = 0
        with self.assertRaises(leg.Abort):
            leg.freeze(process)

    def test_a_dead_source_is_left_alone_by_the_thaw(self):
        process = mock.Mock()
        process.poll.return_value = 1
        leg.thaw(process)
        process.send_signal.assert_not_called()


class Inconclusive(unittest.TestCase):
    """The release-precedence classification: a pinned release
    predating the journaled orphan-episode contract renders a stable
    digest line and a zero exit, and a doctored case over an
    inconclusive run still fails — it can name no evidence."""

    def test_main_reports_the_inconclusive_digest(self):
        rc, out, err = run_main(
            outcome=leg.Inconclusive(
                "the declared standby declares no journal_file",
                "the manifest declares none",
            )
        )
        self.assertEqual(rc, 0)
        self.assertIn(
            "orphan-episode-bound-digest inconclusive — the declared "
            "standby declares no journal_file",
            out,
        )
        # The run's own evidence reports on stderr only.
        self.assertIn("the manifest declares none", err)

    def test_two_inconclusive_passes_render_identically(self):
        first = run_main(outcome=leg.Inconclusive("pre-contract"))
        second = run_main(outcome=leg.Inconclusive("pre-contract"))
        self.assertEqual(first[0], 0)
        self.assertEqual(first[1], second[1])

    def test_a_doctored_case_over_an_inconclusive_run_fails(self):
        rc, _out, err = run_main(
            tamper="expect-flood", outcome=leg.Inconclusive("pre-contract")
        )
        self.assertEqual(rc, 1)
        self.assertIn(leg.TAMPER_EVIDENCE, err)


class Classification(unittest.TestCase):
    """main()'s exit classification over the pass result — failures
    stream to stderr prefixed by the stem, a doctored case may never
    exit zero, and a clean pass renders the sha digest line."""

    def test_failures_report_by_name(self):
        rc, out, err = run_main(
            outcome=([], {}, ["the orphan episode journaled 3 served "
                              "field_orphaned records"])
        )
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn(
            "orphan-episode-bound: the orphan episode journaled 3 "
            "served field_orphaned records",
            err,
        )

    def test_an_abort_reports_by_name(self):
        rc, _out, err = run_main(outcome=leg.Abort("the island never formed"))
        self.assertEqual(rc, 1)
        self.assertIn(
            "orphan-episode-bound: the island never formed", err)

    def test_a_doctored_case_never_passes_silently(self):
        rc, _out, err = run_main(tamper="expect-flood", outcome=([], {}, []))
        self.assertEqual(rc, 1)
        self.assertIn("passed silently", err)

    def test_a_clean_pass_renders_the_digest_line(self):
        evidence = {
            "converged": 4,
            "episode_one": {"served": 1, "durable": 1},
            "episode_two": {"served": 2, "durable": 2},
            "misses": [1, 2, 3],
            "restored_tick": 41,
        }
        rc, out, _err = run_main(
            outcome=([{"phase": "converge"}], evidence, [])
        )
        self.assertEqual(rc, 0)
        self.assertRegex(
            out, r"^orphan-episode-bound-digest [0-9a-f]{64} — "
        )
        self.assertIn("tracking by tick 4", out)
        self.assertIn("counted 1 → 3 toward the armed budget", out)
        self.assertIn("the launch roles stand", out)


if __name__ == "__main__":
    unittest.main()