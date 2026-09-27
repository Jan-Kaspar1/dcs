"""The orphan_retarget_journal leg's unit coverage — ci/legs/
orphan_retarget_journal.py is the reference plant's
consumer-boundary mirror of the rig's orphan-retarget-journal
exercise (the #1137 contract): with the manifest-declared pair
converged and a spawned sibling promoted over the documented
demote path, the declared standby's orphaned applies re-target its
pulls onto the verified successor — the resolution journaling the
attributed `tracking_source_adopted` with parity to the announced
and claimed adoptions, the `field_orphaned` episode it resolved
bracketing it, and the pair reconverging to one active plus one
tracking standby inside the first process lifetime. These tests
pin, without launching the pair: the leg's registration record, its
journal/report projections, and the inconclusive / doctored-case
classifications the harness relies on."""
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
_LEG_PATH = _CI_DIR / "legs" / "orphan_retarget_journal.py"
_SCENARIO_PATH = _CI_DIR / "scenario.json"
_MANIFEST_PATH = _ROOT / "reference-plant" / "deploy" / "manifest.json"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


legs = load(_CI_DIR / "legs.py", "legs")
leg = load(_LEG_PATH, "orphan_retarget_journal")


def argv(tamper=None):
    args = [
        "orphan_retarget_journal.py",
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
    or the exception orphan_retarget_journal_pass raises. Returns
    `(rc, stdout, stderr)`."""
    if isinstance(outcome, BaseException):
        stub = mock.Mock(side_effect=outcome)
    else:
        stub = mock.Mock(return_value=outcome)
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", argv(tamper)), \
            mock.patch.object(
                leg, "orphan_retarget_journal_pass", stub), \
            contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        rc = leg.main()
    return rc, out.getvalue(), err.getvalue()


class Registration(unittest.TestCase):
    """The leg's `LEG` literal — the pair stage's discovery contract:
    the declared order is unique across the directory, the named
    diagnostics follow the issue's `retarget-journal-*` naming under
    the file stem's `-nondeterministic`/`-unchecked` convention,
    and the doctored case declares the evidence the honest run
    reports."""

    def test_the_leg_registers_in_the_pair_stage(self):
        discovered = {
            Path(record["file"]).name: record
            for record in legs.discover(str(_CI_DIR / "legs"))
        }
        record = discovered["orphan_retarget_journal.py"]
        self.assertEqual(record["stem"], "orphan-retarget-journal")
        self.assertEqual(record["order"], 530)

    def test_the_named_diagnostics_follow_the_issue(self):
        # The issue names retarget-journal-failed — the declared
        # `failed` override — while the driver's stem convention
        # emits orphan-retarget-journal-nondeterministic and
        # -unchecked off the file name.
        self.assertEqual(leg.LEG["failed"], "retarget-journal-failed")
        self.assertEqual(leg.LEG["passes"], "retarget-journal-leg")

    def test_the_doctored_case_carries_named_evidence(self):
        tampers = {entry["name"]: entry for entry in leg.LEG["tampers"]}
        self.assertEqual(set(tampers), {"expect-silence"})
        # The declared evidence must be the diagnostic the leg
        # actually prints — pin it against the source's failure
        # lines so a drifted message cannot pass the harness's
        # substring check by accident.
        source = _LEG_PATH.read_text()
        for name, record in tampers.items():
            self.assertTrue(record["evidence"], name)
            for evidence in record["evidence"]:
                self.assertIn(evidence, source, name)
            for field in ("passed", "missed"):
                self.assertIsInstance(record[field], str)


class Projections(unittest.TestCase):
    """The journal and report projections the audit reads — the leg
    shares the tracking-source-fallback mirror's projections: the
    adoption records as (seq, record) pairs in seq order, the sync
    vocabulary off a report, and the pair member an address resolves
    to by monitor port, the durable file's boundary and event-kind
    reads beside them."""

    def test_the_leg_reuses_the_mirror_siblings_projections(self):
        for name in (
            "journal_entries",
            "durable_kinds",
            "journal_boundaries",
            "adopted_entries",
            "adopted_kinds",
            "sync_kind",
            "monitor_port",
            "peer_kind",
        ):
            self.assertIs(
                getattr(leg, name),
                getattr(leg.tracking_source_fallback, name),
                name,
            )

    def test_adopted_entries_project_in_seq_order(self):
        entries = [
            {"seq": 3, "tick": 7, "event": {"tracking_source_adopted": {
                "source": "127.0.0.1:9001"}}},
            {"seq": 4, "tick": 8, "event": {"role_changed": {
                "from": "standby", "to": "standby"}}},
            {"seq": 5, "tick": 9, "event": {"tracking_source_adopted": {
                "source": "127.0.0.1:9003"}}},
        ]
        self.assertEqual(
            leg.adopted_entries(entries),
            [(3, {"source": "127.0.0.1:9001"}),
             (5, {"source": "127.0.0.1:9003"})],
        )

    def test_journal_projections_read_the_durable_file(self):
        records = [
            {"run_boundary": {"run": 1, "tick": 0}},
            {"entry": {"seq": 1, "tick": 2, "event": {
                "field_orphaned": {"aligned": 5}}}},
            {"entry": {"seq": 2, "tick": 5, "event": {
                "tracking_source_adopted": {
                    "source": "127.0.0.1:9003"}}}},
        ]
        with tempfile.NamedTemporaryFile(
            "w", suffix=".jsonl", delete=False
        ) as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
            path = handle.name
        self.assertEqual(
            leg.journal_boundaries(path), [{"run": 1, "tick": 0}])
        self.assertEqual(
            leg.durable_kinds(path),
            {"field_orphaned", "tracking_source_adopted"},
        )


class Inconclusive(unittest.TestCase):
    """The release-precedence classification: a pinned release
    predating the journaled-retarget contract — the re-target
    landing unjournaled being the finding's own reproduction —
    raises the leg's Inconclusive, which main renders as a stable
    digest line and a zero exit — and a doctored case over an
    inconclusive run still fails, since it can name no evidence."""

    def test_main_reports_the_inconclusive_digest(self):
        rc, out, err = run_main(
            outcome=leg.Inconclusive(
                "the standby re-targeted its pulls onto the "
                "successor unjournaled"
            )
        )
        self.assertEqual(rc, 0)
        self.assertIn(
            "orphan-retarget-journal-digest inconclusive — the "
            "standby re-targeted",
            out,
        )
        self.assertIn("orphan-retarget-journal: inconclusive", err)

    def test_two_inconclusive_passes_render_identically(self):
        first = run_main(outcome=leg.Inconclusive("pre-contract"))
        second = run_main(outcome=leg.Inconclusive("pre-contract"))
        self.assertEqual(first[0], 0)
        self.assertEqual(first[1], second[1])

    def test_a_doctored_case_over_an_inconclusive_run_fails(self):
        rc, out, err = run_main(
            tamper="expect-silence",
            outcome=leg.Inconclusive("pre-contract"),
        )
        self.assertEqual(rc, 1)
        self.assertIn(
            "the doctored expectation wanted the re-target "
            "unjournaled",
            err,
        )


class Classification(unittest.TestCase):
    """main()'s exit classification over the pass result — failures
    stream to stderr prefixed by the stem, a doctored case may never
    exit zero, and a clean pass renders the sha digest line."""

    def test_failures_report_by_name(self):
        rc, out, err = run_main(
            outcome=([], {}, ["the durable journal file carries no "
                             "field_orphaned record"])
        )
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn(
            "orphan-retarget-journal: the durable journal file "
            "carries no field_orphaned record",
            err,
        )

    def test_an_abort_reports_by_name(self):
        rc, out, err = run_main(
            outcome=leg.Abort("the pair never converged"))
        self.assertEqual(rc, 1)
        self.assertIn(
            "orphan-retarget-journal: the pair never converged", err)

    def test_a_doctored_case_never_passes_silently(self):
        rc, out, err = run_main(
            tamper="expect-silence", outcome=([], {}, [])
        )
        self.assertEqual(rc, 1)
        self.assertIn("passed silently", err)

    def test_a_doctored_case_carrying_failures_still_fails(self):
        rc, _, _ = run_main(
            tamper="expect-silence",
            outcome=([], {}, ["the honest run journaled the adoption"]),
        )
        self.assertEqual(rc, 1)

    def test_a_clean_pass_renders_the_digest_line(self):
        evidence = {
            "converged": 4,
            "restored_at": 21,
        }
        rc, out, err = run_main(
            outcome=([{"phase": "converge"}], evidence, [])
        )
        self.assertEqual(rc, 0)
        self.assertRegex(
            out, r"^orphan-retarget-journal-digest [0-9a-f]{64} — ")
        self.assertIn("tracking by tick 4", out)
        self.assertIn("launch roles restored at tick 21", out)


if __name__ == "__main__":
    unittest.main()
