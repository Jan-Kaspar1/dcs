"""The announced_source_verify leg's unit coverage — ci/legs/
announced_source_verify.py is the reference plant's consumer-boundary
mirror of the rig's announced-source-verify exercise (#867's settled
contract): with the foreign endpoint's `?peer=` announce the only
recorded tracking hint, `POST /demote` must answer the named
`no_tracking_source`, spend only a bounded verify pass at the unproven
endpoint, journal the refused probe by name, and never adopt it — while
the same forge's hint is still refused, and the legitimate follow-peer
path still open, beside a genuine announce. These tests pin, without
launching the pair: the leg's registration record, its foreign-endpoint
staging and journal/report projections, the inconclusive and
doctored-case classifications the harness relies on, and the
self-check the sibling announced-hint legs import it for."""
import contextlib
import importlib
import io
import json
import re
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
_CI_DIR = _ROOT / "reference-plant" / "ci"
_LEGS_DIR = _CI_DIR / "legs"
_LEG_PATH = _LEGS_DIR / "announced_source_verify.py"
_SCENARIO_PATH = _CI_DIR / "scenario.json"
_MANIFEST_PATH = _ROOT / "reference-plant" / "deploy" / "manifest.json"


def load(path, name):
    """The module at `path` under its own file stem — the same name a
    leg's siblings import it by, so the shared staging is one object
    rather than one copy per import."""
    for parent in (str(_LEGS_DIR), str(_CI_DIR)):
        if parent not in sys.path:
            sys.path.insert(0, parent)
    sys.modules.pop(name, None)
    return importlib.import_module(name)


def normalized_source(path):
    """A source file's text with its implicit string-literal joins and
    runs of whitespace collapsed — the declared doctored-case evidence
    is one phrase, and a leg is free to wrap it across adjacent
    literals or lines without the evidence ceasing to name it."""
    text = re.sub(r'"\s*"', "", path.read_text())
    return re.sub(r"\s+", " ", text)


legs = load(_CI_DIR / "legs.py", "legs")
leg = load(_LEG_PATH, "announced_source_verify")


def argv(tamper=None):
    args = [
        "announced_source_verify.py",
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
    """`main()` against a stubbed pass — `outcome` the return value or
    the exception announced_source_verify_pass raises. Returns
    `(rc, stdout, stderr)`."""
    if isinstance(outcome, BaseException):
        stub = mock.Mock(side_effect=outcome)
    else:
        stub = mock.Mock(return_value=outcome)
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", argv(tamper)), \
            mock.patch.object(leg, "announced_source_verify_pass", stub), \
            contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        rc = leg.main()
    return rc, out.getvalue(), err.getvalue()


def journal_file(records):
    """A `--journal-file` carrying `records` — each either a
    `run_boundary` body or an `event` body."""
    path = Path(tempfile.mkdtemp()) / "journal.jsonl"
    with open(path, "w") as handle:
        handle.write(json.dumps({"run_boundary": {"run": 1, "tick": 0}}) + "\n")
        for index, event in enumerate(records, 1):
            handle.write(
                json.dumps({"entry": {"seq": index, "tick": index,
                                      "event": event}}) + "\n"
            )
    return path


OWN = {
    "format_version": 3,
    "generation": 42,
    "tick": 12,
    "source_owns_field": True,
    "line_owner": "127.0.0.1:9001",
    "line_proof": {"nonce": 7, "mac": "x"},
    "receipts": [],
}


class Registration(unittest.TestCase):
    """The leg's `LEG` literal — the pair stage's discovery contract:
    a unique declared order, the stem-derived diagnostics the issue
    names, and a doctored case whose declared evidence is the line the
    leg actually prints."""

    def test_the_leg_registers_in_the_pair_stage(self):
        discovered = {
            Path(record["file"]).name: record
            for record in legs.discover(str(_LEGS_DIR))
        }
        record = discovered["announced_source_verify.py"]
        self.assertEqual(record["stem"], "announced-source-verify")
        self.assertEqual(record["order"], 121)
        self.assertEqual(record["passes"], "announced-source-verify")

    def test_the_sibling_legs_import_the_shared_staging(self):
        # The hostile endpoint is staged once: the tracking-source-auth
        # and involuntary-demote-verify legs import it rather than
        # standing their own, so the pull ledger one leg records is the
        # ledger the others assert against.
        for name in ("tracking_source_auth", "involuntary_demote_verify"):
            sibling = load(_LEGS_DIR / f"{name}.py", name)
            self.assertTrue(
                sibling.ForeignEndpoint is leg.ForeignEndpoint, name
            )
            self.assertTrue(
                sibling.forged_document is leg.forged_document, name
            )

    def test_the_doctored_case_carries_named_evidence(self):
        tampers = {record["name"]: record for record in leg.LEG["tampers"]}
        self.assertEqual(set(tampers), {"expect-adopted"})
        source = normalized_source(_LEG_PATH)
        for name, record in tampers.items():
            for evidence in record["evidence"]:
                self.assertTrue(
                    evidence in source,
                    f"{name} declares evidence the leg never prints: "
                    f"{evidence!r}",
                )

    def test_the_contract_declares_the_emitted_diagnostics(self):
        contract = (_ROOT / "docs" / "release-contract.md").read_text()
        for name in (
            "`announced-source-verify-failed`",
            "`announced-source-verify-nondeterministic`",
            "`announced-source-verify-unchecked`",
        ):
            self.assertIn(name, contract)


class VerdictClassification(unittest.TestCase):
    """`main()`'s three verdicts — a green digest line, the named
    inconclusive marker that exits zero on a pinned release predating
    the contract, and a nonzero exit on any contract violation or on a
    doctored case that cannot be satisfied."""

    EVIDENCE = {
        "announced_at": 4,
        "refusal": {"verify_pulls": 1},
        "converged": 12,
        "switch": {"handover": [15, 18]},
        "restored": 24,
    }

    def test_a_clean_pass_prints_the_digest_line(self):
        entries = [{"phase": "refusal", "status": 409}]
        rc, out, err = run_main(
            outcome=(entries, self.EVIDENCE, []))
        self.assertEqual(rc, 0)
        self.assertIn("announced-source-verify-digest ", out)
        self.assertIn("no_tracking_source", out)
        self.assertNotIn("inconclusive", out)
        self.assertEqual(err, "")

    def test_a_contract_violation_exits_nonzero_by_name(self):
        rc, out, err = run_main(
            outcome=([{"phase": "refusal"}], self.EVIDENCE,
                     ["the forged announce was adopted"]))
        self.assertEqual(rc, 1)
        self.assertNotIn("announced-source-verify-digest ", out)
        self.assertIn("announced-source-verify: the forged announce was adopted", err)

    def test_a_predating_release_reports_inconclusive_and_exits_zero(self):
        rc, out, err = run_main(
            outcome=leg.Inconclusive("the pinned release predates it"))
        self.assertEqual(rc, 0)
        self.assertEqual(
            out.strip(),
            "announced-source-verify-digest inconclusive — "
            "the pinned release predates it",
        )
        self.assertIn("inconclusive", err)

    def test_the_inconclusive_marker_is_identical_across_passes(self):
        # ci/legs.py compares the two passes' stdout — the marker is the
        # stable phrase, so its wording must not carry a run's own
        # verdicts, tokens, or ephemeral endpoints.
        first = run_main(outcome=leg.Inconclusive("the guard is absent"))[1]
        second = run_main(outcome=leg.Inconclusive("the guard is absent"))[1]
        self.assertEqual(first, second)

    def test_a_doctored_case_offers_an_inconclusive_run_no_evidence(self):
        rc, out, err = run_main(
            tamper="expect-adopted",
            outcome=leg.Inconclusive("the pinned release predates it"),
        )
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("offers the doctored case no evidence", err)

    def test_a_doctored_case_that_satisfies_the_doctor_exits_nonzero(self):
        rc, out, err = run_main(
            tamper="expect-adopted", outcome=([], {}, []))
        self.assertEqual(rc, 1)
        self.assertIn("passed silently", err)


class ForeignDocument(unittest.TestCase):
    """`forged_document` — the hostile document is the victim's own
    bytes replayed as a standby-shaped continuation, so the forge is
    exactly what any reader of the deployment's public `/checkpoint`
    could derive. A checkpoint that is not a field-owning document has
    no such shape, and the leg reports that as a predating release."""

    def test_the_forgery_replays_the_owner_as_a_standby(self):
        forged = leg.forged_document(OWN)
        self.assertEqual(forged["tick"], OWN["tick"])
        self.assertEqual(forged["generation"], OWN["generation"])
        self.assertIs(forged["source_owns_field"], False)
        self.assertNotIn("line_owner", forged)
        self.assertNotIn("line_proof", forged)

    def test_the_forgery_leaves_the_victim_document_untouched(self):
        victim = json.loads(json.dumps(OWN))
        leg.forged_document(victim)
        self.assertEqual(victim, OWN)

    def test_a_document_claiming_no_field_has_no_forgery(self):
        self.assertIsNone(leg.forged_document({"tick": 1}))
        self.assertIsNone(
            leg.forged_document({"source_owns_field": False})
        )
        self.assertIsNone(leg.forged_document(None))


class ForeignEndpoint(unittest.TestCase):
    """The shared hostile endpoint: a loopback server serving one staged
    document, ledgering every pull, and announcing itself through the
    documented `?peer=` mechanism. The pull ledger is the record of what
    a tracking path dialed at an unproven endpoint, so its count and its
    `?prove=` query are both asserted."""

    def test_it_serves_the_staged_document_and_ledgers_the_pull(self):
        endpoint = leg.ForeignEndpoint({"tick": 3})
        try:
            self.assertEqual(
                json.load(urllib.request.urlopen(f"http://{endpoint.address}/checkpoint")),
                {"tick": 3},
            )
            hits = endpoint.served_pulls()
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]["path"], "/checkpoint")
            # The verify pull asks for the keyed proof; the endpoint is
            # outside the line and never answers one.
            self.assertEqual(hits[0]["query"], "")
        finally:
            endpoint.stop()

    def test_a_prove_query_is_ledgered_and_never_answered(self):
        endpoint = leg.ForeignEndpoint({"tick": 3})
        try:
            urllib.request.urlopen(
                f"http://{endpoint.address}/checkpoint?prove=99"
            ).read()
            hits = endpoint.served_pulls()
            self.assertEqual(hits[0]["query"], "prove=99")
        finally:
            endpoint.stop()

    def test_staging_replaces_the_served_document(self):
        endpoint = leg.ForeignEndpoint({"tick": 3})
        try:
            endpoint.stage({"tick": 9})
            self.assertEqual(
                json.load(urllib.request.urlopen(f"http://{endpoint.address}/checkpoint")),
                {"tick": 9},
            )
            self.assertEqual(len(endpoint.served_pulls()), 1)
        finally:
            endpoint.stop()

    def test_the_announce_names_the_endpoints_own_source_address(self):
        # #684's acceptance half: a serving monitor records an announce
        # only when it names the pulling connection's own source
        # address. The stand-in announces its own loopback address, the
        # one source a process outside the deployment is genuinely
        # reached from, so the announce lands — the hostile case.
        endpoint = leg.ForeignEndpoint(OWN)
        try:
            host, _, port = endpoint.address.partition(":")
            self.assertEqual(host, "127.0.0.1")
            self.assertEqual(port, str(endpoint.port))
            answered = endpoint.announce(
                f"http://{endpoint.address}", []
            )
            self.assertEqual(answered["tick"], OWN["tick"])
        finally:
            endpoint.stop()

    def test_the_announce_unwinds_when_the_serving_peer_serves_nothing(self):
        endpoint = leg.ForeignEndpoint({})
        failures = []
        try:
            with self.assertRaises(leg.Abort):
                endpoint.announce(f"http://{endpoint.address}", failures)
            self.assertIn("served no document", failures[0])
        finally:
            endpoint.stop()

    def test_the_pull_ledger_is_a_suffix_of_the_whole(self):
        endpoint = leg.ForeignEndpoint(OWN)
        try:
            urllib.request.urlopen(
                f"http://{endpoint.address}/checkpoint"
            ).read()
            urllib.request.urlopen(
                f"http://{endpoint.address}/checkpoint"
            ).read()
            self.assertEqual(len(endpoint.served_pulls()), 2)
            self.assertEqual(len(endpoint.served_pulls(1)), 1)
        finally:
            endpoint.stop()


class JournalProjections(unittest.TestCase):
    """The durable half: the refused probes and the adoptions the
    `--journal-file` carries. `pair.journal_records` elides an adopted
    address's port for digest stability, so the adoption's endpoint
    identity is read through `adopted_sources` — a port-level assertion
    that silently compared normalized records would pass on the wrong
    endpoint."""

    def test_refusals_read_only_the_records_since_the_floor(self):
        path = journal_file([
            {"tracking_source_refused": {"source": "127.0.0.1:1",
                                         "detail": "early"}},
            {"tracking_source_refused": {"source": "127.0.0.1:2",
                                         "detail": "late"}},
        ])
        self.assertEqual(
            [detail for _s, detail in leg.refusals(path)],
            ["early", "late"],
        )
        # Records are the cold-start boundary, then one per event — a
        # floor past the boundary leaves only the episode's own.
        self.assertEqual(
            leg.refusals(path, 2), [("127.0.0.1:2", "late")]
        )
        self.assertEqual(leg.refusals(path, 3), [])

    def test_a_file_without_a_refusal_reads_empty(self):
        self.assertEqual(leg.refusals(journal_file([{"role_changed": {}}])), [])

    def test_adoptions_keep_the_port_the_normalizing_projection_drops(self):
        path = journal_file([
            {"tracking_source_adopted": {"source": "127.0.0.1:8081"}},
        ])
        self.assertEqual(leg.adopted_sources(path), ["127.0.0.1:8081"])
        self.assertEqual(leg.adopted_ports(path), ["8081"])
        # The shared projection the digest uses elides the port — the
        # reason the leg reads the adoption's endpoint identity itself.
        self.assertEqual(
            [record["event"]["tracking_source_adopted"]["source"]
             for _kind, record in leg.pair.journal_records(path)
             if "tracking_source_adopted" in (record.get("event") or {})],
            ["127.0.0.1"],
        )


class ManifestAudit(unittest.TestCase):
    def test_a_pair_declaring_no_journal_is_reported_as_predating(self):
        declared = (None,
                    {"name": "ctrl-a", "journal_file": "/var/tmp/j.jsonl"},
                    {"name": "ctrl-b"})
        self.assertFalse(leg.rig_journal_absent(declared))
        declared = (None,
                    {"name": "ctrl-a"},
                    {"name": "ctrl-b", "journal_file": "/var/tmp/j.jsonl"})
        self.assertFalse(leg.rig_journal_absent(declared))
        self.assertTrue(
            leg.rig_journal_absent((None, {"name": "ctrl-a"},
                                    {"name": "ctrl-b"}))
        )


if __name__ == "__main__":
    unittest.main()
