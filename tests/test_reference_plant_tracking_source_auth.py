"""The tracking_source_auth leg's unit coverage — ci/legs/
tracking_source_auth.py is the reference plant's consumer-boundary
mirror of the rig's tracking-source-auth exercise (#684's settled
announcement-authenticity contract): a fabricated `?peer=` hint must
not unblock the demote guard on the deployment's unsourced instance,
must not silently redirect the tracking standby's already-established
`--standby` source, and must never inject a forged checkpoint without a
journal entry — while the pair's own legitimate announces keep it
tracking. These tests pin, without launching the pair: the leg's
registration record, its announce-variant staging, and the
inconclusive and doctored-case classifications the harness relies on."""
import contextlib
import importlib
import io
import json
import re
import socket
import sys
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
_CI_DIR = _ROOT / "reference-plant" / "ci"
_LEGS_DIR = _CI_DIR / "legs"
_LEG_PATH = _LEGS_DIR / "tracking_source_auth.py"
_SCENARIO_PATH = _CI_DIR / "scenario.json"
_MANIFEST_PATH = _ROOT / "reference-plant" / "deploy" / "manifest.json"


def load(path, name):
    for parent in (str(_LEGS_DIR), str(_CI_DIR)):
        if parent not in sys.path:
            sys.path.insert(0, parent)
    sys.modules.pop(name, None)
    return importlib.import_module(name)


def normalized_source(path):
    text = re.sub(r'"\s*"', "", path.read_text())
    return re.sub(r"\s+", " ", text)


legs = load(_CI_DIR / "legs.py", "legs")
leg = load(_LEG_PATH, "tracking_source_auth")


def argv(tamper=None):
    args = [
        "tracking_source_auth.py",
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
    if isinstance(outcome, BaseException):
        stub = mock.Mock(side_effect=outcome)
    else:
        stub = mock.Mock(return_value=outcome)
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", argv(tamper)), \
            mock.patch.object(leg, "tracking_source_auth_pass", stub), \
            contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        rc = leg.main()
    return rc, out.getvalue(), err.getvalue()


class _Checkpoint(BaseHTTPRequestHandler):
    """A serving monitor stand-in answering `{"tick": N}` for every
    read — the announce read and the plain read are indistinguishable
    from the client's side, which is what makes a refused announce
    silent."""

    tick = 7

    def do_GET(self):
        body = json.dumps({"tick": _Checkpoint.tick}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class serving_monitor:
    """A loopback serving-monitor stand-in for the read helpers."""

    def __enter__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Checkpoint)
        self.thread = __import__("threading").Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class Registration(unittest.TestCase):
    def test_the_leg_registers_in_the_pair_stage(self):
        discovered = {
            Path(record["file"]).name: record
            for record in legs.discover(str(_LEGS_DIR))
        }
        record = discovered["tracking_source_auth.py"]
        self.assertEqual(record["stem"], "tracking-source-auth")
        self.assertEqual(record["order"], 122)
        self.assertEqual(record["passes"], "tracking-source-auth")

    def test_the_announced_hint_legs_share_the_hostile_endpoint(self):
        # The staging is imported, not restated: the pull ledger the
        # announced-source-verify leg records is the ledger this leg
        # asserts its bounded verify pass against.
        shared = importlib.import_module("announced_source_verify")
        self.assertTrue(leg.ForeignEndpoint is shared.ForeignEndpoint)
        self.assertTrue(leg.forged_document is shared.forged_document)

    def test_the_doctored_case_carries_named_evidence(self):
        tampers = {record["name"]: record for record in leg.LEG["tampers"]}
        self.assertEqual(set(tampers), {"expect-unblocked"})
        source = normalized_source(_LEG_PATH)
        for name, record in tampers.items():
            for evidence in record["evidence"]:
                self.assertTrue(
                    evidence in source,
                    f"{name} declares evidence the leg never prints: "
                    f"{evidence!r}",
                )


class VerdictClassification(unittest.TestCase):
    EVIDENCE = {
        "verify_pulls": 1,
        "converged": 12,
        "restored": 24,
    }

    def test_a_clean_pass_prints_the_digest_line(self):
        rc, out, err = run_main(
            outcome=([{"phase": "guard"}], self.EVIDENCE, []))
        self.assertEqual(rc, 0)
        self.assertIn("tracking-source-auth-digest ", out)
        self.assertIn("no_tracking_source", out)
        self.assertEqual(err, "")

    def test_a_contract_violation_exits_nonzero_by_name(self):
        rc, out, err = run_main(
            outcome=([], self.EVIDENCE, ["a redirect retargeted the pulls"]))
        self.assertEqual(rc, 1)
        self.assertNotIn("tracking-source-auth-digest ", out)
        self.assertIn(
            "tracking-source-auth: a redirect retargeted the pulls", err
        )

    def test_a_predating_release_reports_inconclusive_and_exits_zero(self):
        rc, out, err = run_main(
            outcome=leg.Inconclusive("the pinned release predates it"))
        self.assertEqual(rc, 0)
        self.assertEqual(
            out.strip(),
            "tracking-source-auth-digest inconclusive — "
            "the pinned release predates it",
        )
        self.assertIn("inconclusive", err)

    def test_a_doctored_case_offers_an_inconclusive_run_no_evidence(self):
        rc, out, err = run_main(
            tamper="expect-unblocked",
            outcome=leg.Inconclusive("the pinned release predates it"),
        )
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("offers the doctored case no evidence", err)

    def test_a_doctored_case_that_satisfies_the_doctor_exits_nonzero(self):
        rc, out, err = run_main(
            tamper="expect-unblocked", outcome=([], {}, []))
        self.assertEqual(rc, 1)
        self.assertIn("passed silently", err)


class AnnounceVariants(unittest.TestCase):
    """The three `?peer=` variants the sweep drives. The crafted one
    names an address on a foreign IP at a just-released port — off the
    pulling connection's own loopback source, so the serving monitor
    refuses it — which is the seam's acceptance half: a refused announce
    is silently ignored and the checkpoint read is unchanged."""

    def test_the_crafted_announce_names_a_foreign_host_and_a_dead_port(self):
        announce = leg.foreign_announce()
        host, _, port = announce.rpartition(":")
        self.assertEqual(host, leg.FOREIGN_HOST)
        self.assertNotEqual(host, "127.0.0.1")
        stream = socket.socket()
        try:
            with self.assertRaises(ConnectionRefusedError):
                stream.connect(("127.0.0.1", int(port)))
        finally:
            stream.close()

    def test_each_sweep_gets_its_own_closed_port(self):
        self.assertNotEqual(leg.foreign_announce(), leg.foreign_announce())

    def test_the_served_checkpoint_read_is_the_own_document(self):
        with serving_monitor() as url:
            served = leg.served_checkpoint(url, [])
        self.assertEqual(served, {"tick": _Checkpoint.tick})

    def test_a_serving_peer_that_serves_nothing_unwinds(self):
        # `pair.get` records the transport failure and unwinds on its
        # own, so the read never returns a half-answered document.
        failures = []
        with mock.patch.object(
            leg.pair.simulate, "http",
            side_effect=OSError("connection refused"),
        ):
            with self.assertRaises(leg.Abort):
                leg.served_checkpoint("http://127.0.0.1:1", failures)
        self.assertIn("connection refused", failures[0])

    def test_a_non_document_answer_unwinds_with_its_own_reason(self):
        with mock.patch.object(leg.pair, "get", return_value=None):
            with self.assertRaises(leg.Abort):
                leg.served_checkpoint("http://127.0.0.1:1", [])
        # A monitor answering something other than a checkpoint document
        # is the harness admitting no announced-source staging at all.

    def test_the_guard_answer_returns_a_refusal_without_raising(self):
        with mock.patch.object(leg.pair, "request",
                               return_value=(409, "no_tracking_source")) as post:
            self.assertEqual(
                leg.guard_answer("http://127.0.0.1:1", []),
                (409, "no_tracking_source"),
            )
        post.assert_called_once_with("http://127.0.0.1:1/demote", {})


if __name__ == "__main__":
    unittest.main()
