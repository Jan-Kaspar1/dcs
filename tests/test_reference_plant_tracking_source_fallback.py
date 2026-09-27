"""The tracking_source_fallback leg's unit coverage — ci/legs/
tracking_source_fallback.py is the reference plant's
consumer-boundary mirror of the rig's tracking-source-fallback
exercise (the #1136 contract): with the manifest-declared pair
converged and a spawned sibling promoted over the documented
demote path, the declared standby's learned pin on the successor
must release inside its miss bound when the pinned endpoint dies
while the configured `--standby` source stays live — the
released re-resolution journaling `tracking_source_adopted`
naming the configured source and the peer reconverging
`tracking` inside its first process lifetime. These tests pin,
without launching the pair: the leg's registration record, its
journal/report projections, and the inconclusive /
doctored-case classifications the harness relies on."""
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
_LEG_PATH = _CI_DIR / "legs" / "tracking_source_fallback.py"
_SCENARIO_PATH = _CI_DIR / "scenario.json"
_MANIFEST_PATH = _ROOT / "reference-plant" / "deploy" / "manifest.json"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


legs = load(_CI_DIR / "legs.py", "legs")
leg = load(_LEG_PATH, "tracking_source_fallback")


def argv(tamper=None):
    args = [
        "tracking_source_fallback.py",
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
    or the exception tracking_source_fallback_pass raises. Returns
    `(rc, stdout, stderr)`."""
    if isinstance(outcome, BaseException):
        stub = mock.Mock(side_effect=outcome)
    else:
        stub = mock.Mock(return_value=outcome)
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", argv(tamper)), \
            mock.patch.object(
                leg, "tracking_source_fallback_pass", stub), \
            contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        rc = leg.main()
    return rc, out.getvalue(), err.getvalue()


def entry(seq, tick, event):
    return {"seq": seq, "tick": tick, "event": event}


URLS = {
    "duty": "http://127.0.0.1:9001",
    "standby": "http://127.0.0.1:9002",
    "successor": "http://127.0.0.1:9003",
}


class Registration(unittest.TestCase):
    """The leg's `LEG` literal — the pair stage's discovery contract:
    the declared order is unique across the directory, the named
    diagnostics follow the issue's `source-fallback-*` naming under
    the file stem's `-nondeterministic`/`-unchecked` convention,
    and the doctored case declares the evidence the honest run
    reports."""

    def test_the_leg_registers_in_the_pair_stage(self):
        discovered = {
            Path(record["file"]).name: record
            for record in legs.discover(str(_CI_DIR / "legs"))
        }
        record = discovered["tracking_source_fallback.py"]
        self.assertEqual(record["stem"], "tracking-source-fallback")
        self.assertEqual(record["order"], 520)

    def test_the_named_diagnostics_follow_the_issue(self):
        # The issue names source-fallback-failed — the declared
        # `failed` override — while the driver's stem convention
        # emits tracking-source-fallback-nondeterministic and
        # -unchecked off the file name.
        self.assertEqual(leg.LEG["failed"], "source-fallback-failed")
        self.assertEqual(leg.LEG["passes"], "source-fallback-leg")

    def test_the_doctored_case_carries_named_evidence(self):
        tampers = {entry["name"]: entry for entry in leg.LEG["tampers"]}
        self.assertEqual(set(tampers), {"expect-stranded"})
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
    """The journal and report projections the audit reads — the
    adoption records as (seq, record) pairs in seq order, the sync
    vocabulary off a report, the pull target a `degraded` detail
    names, and the pair member an address resolves to by monitor
    port."""

    def test_adopted_entries_project_in_seq_order(self):
        entries = [
            entry(3, 7, {"tracking_source_adopted": {
                "source": "127.0.0.1:9001"}}),
            entry(4, 8, {"role_changed": {
                "from": "standby", "to": "standby"}}),
            entry(5, 9, {"tracking_source_adopted": {
                "source": "127.0.0.1:9003"}}),
        ]
        self.assertEqual(
            leg.adopted_entries(entries),
            [(3, {"source": "127.0.0.1:9001"}),
             (5, {"source": "127.0.0.1:9003"})],
        )

    def test_sync_kind_reads_the_string_and_variant_forms(self):
        self.assertEqual(
            leg.sync_kind({"sync": "unsynchronized"}),
            "unsynchronized")
        self.assertEqual(
            leg.sync_kind({"sync": {"tracking": {"aligned": 12}}}),
            "tracking")
        self.assertEqual(
            leg.sync_kind({"sync": {"degraded": {"detail": "…"}}}),
            "degraded")
        self.assertEqual(leg.sync_kind({"role": "active"}), "missing")

    def test_monitor_port_reads_the_endpoint_suffix(self):
        self.assertEqual(
            leg.monitor_port("http://127.0.0.1:9001"), "9001")
        self.assertEqual(leg.monitor_port("127.0.0.1:8080"), "8080")

    def test_peer_kind_classifies_by_monitor_port(self):
        self.assertEqual(
            leg.peer_kind(URLS, "127.0.0.1:9001"), "duty")
        self.assertEqual(
            leg.peer_kind(URLS, "127.0.0.1:9002"), "standby")
        self.assertEqual(
            leg.peer_kind(URLS, "127.0.0.1:9003"), "successor")
        self.assertEqual(leg.peer_kind(URLS, "127.0.0.1:9999"), "foreign")

    def test_degraded_target_reads_the_fetch_source(self):
        report = {"sync": {"degraded": {
            "detail": "fetch from 127.0.0.1:9003: connection refused"}}}
        self.assertEqual(
            leg.degraded_target(report), "127.0.0.1:9003")
        self.assertIsNone(
            leg.degraded_target({"sync": {"tracking": {}}}))
        self.assertIsNone(leg.degraded_target({"sync": "degraded"}))

    def test_adopted_kinds_classifies_the_pin_sequence(self):
        entries = [
            entry(3, 7, {"tracking_source_adopted": {
                "source": "127.0.0.1:9001"}}),
            entry(4, 8, {"tracking_source_adopted": {
                "source": "127.0.0.1:9003"}}),
            entry(5, 9, {"tracking_source_adopted": {
                "source": "127.0.0.1:9001"}}),
        ]
        self.assertEqual(
            leg.adopted_kinds(URLS, entries),
            ["duty", "successor", "duty"],
        )

    def test_journal_projections_read_the_durable_file(self):
        records = [
            {"run_boundary": {"run": 1, "tick": 0}},
            {"entry": entry(1, 2, {"field_orphaned": {"source": "x"}})},
            {"entry": entry(2, 5, {"tracking_source_adopted": {
                "source": "127.0.0.1:9003"}})},
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
        entries = leg.journal_entries(path)
        self.assertEqual(
            leg.adopted_entries(entries),
            [(2, {"source": "127.0.0.1"}),],  # the port elided
        )


class Inconclusive(unittest.TestCase):
    """The release-precedence classification: a pinned release
    predating the learned-pin fallback contract raises the leg's
    Inconclusive, which main renders as a stable digest line and a
    zero exit — and a doctored case over an inconclusive run still
    fails, since it can name no evidence."""

    def test_main_reports_the_inconclusive_digest(self):
        rc, out, err = run_main(
            outcome=leg.Inconclusive(
                "the pinned release predates the dead-source "
                "fallback contract"
            )
        )
        self.assertEqual(rc, 0)
        self.assertIn(
            "tracking-source-fallback-digest inconclusive — the "
            "pinned release predates",
            out,
        )
        self.assertIn("tracking-source-fallback: inconclusive", err)

    def test_two_inconclusive_passes_render_identically(self):
        first = run_main(outcome=leg.Inconclusive("pre-contract"))
        second = run_main(outcome=leg.Inconclusive("pre-contract"))
        self.assertEqual(first[0], 0)
        self.assertEqual(first[1], second[1])

    def test_a_doctored_case_over_an_inconclusive_run_fails(self):
        rc, out, err = run_main(
            tamper="expect-stranded",
            outcome=leg.Inconclusive("pre-contract"),
        )
        self.assertEqual(rc, 1)
        self.assertIn(
            "the doctored expectation wanted the standby stranded "
            "on the dead pinned source",
            err,
        )


class Classification(unittest.TestCase):
    """main()'s exit classification over the pass result — failures
    stream to stderr prefixed by the stem, a doctored case may never
    exit zero, and a clean pass renders the sha digest line."""

    def test_failures_report_by_name(self):
        rc, out, err = run_main(
            outcome=([], {}, ["the tracking peer never reconverged"])
        )
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn(
            "tracking-source-fallback: the tracking peer never "
            "reconverged",
            err,
        )

    def test_an_abort_reports_by_name(self):
        rc, out, err = run_main(
            outcome=leg.Abort("the pair never converged")
        )
        self.assertEqual(rc, 1)
        self.assertIn(
            "tracking-source-fallback: the pair never converged", err)

    def test_a_doctored_case_never_passes_silently(self):
        rc, out, err = run_main(
            tamper="expect-stranded", outcome=([], {}, [])
        )
        self.assertEqual(rc, 1)
        self.assertIn("passed silently", err)

    def test_a_doctored_case_carrying_failures_still_fails(self):
        rc, _, _ = run_main(
            tamper="expect-stranded",
            outcome=([], {}, ["the honest run released the pin"]),
        )
        self.assertEqual(rc, 1)

    def test_a_clean_pass_renders_the_digest_line(self):
        evidence = {
            "converged": 4,
            "reconverged": 13,
            "restored_at": 13,
        }
        rc, out, err = run_main(
            outcome=([{"phase": "converge"}], evidence, [])
        )
        self.assertEqual(rc, 0)
        self.assertRegex(
            out, r"^tracking-source-fallback-digest [0-9a-f]{64} — ")
        self.assertIn("tracking by tick 4", out)
        self.assertIn("configured source at tick 13", out)
        self.assertIn("launch roles restored at tick 13", out)


if __name__ == "__main__":
    unittest.main()
