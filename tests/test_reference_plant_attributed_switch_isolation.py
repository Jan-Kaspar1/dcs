"""The attributed-switch-isolation leg's decision seam, unit-tested:
`isolation_verdict` reads the leg's two control-lane samples under the
staged congestion into the three verdicts the leg's contract rests on —
`held` (the attributed switch answered beside the pinned submission
lane), `queued-behind` (the pre-contract routing: it did not answer
while the bodiless control request on the same monitor did, so the
pinned release predates the contract and the run reports inconclusive),
and `control-lane-silent` (neither shape answered, so the window holds
no control-lane answer to read the attributed switch's lane against and
the lateness is a failure, never a contract inference).

These tests pin that discrimination, the verdict rendering an answered
switch request's `200`/`409` bodies take, the pinning requests the leg
stages (each a `POST /scan` — the submission lane's own path — whose
declared body the client never sends, and a set past the lane's worker
count), and the `<stem>-unchecked` self-check's evidence phrase: the
rendered outcome of a switch that answered inside no bound carries the
substring `ci/legs.py` requires the doctored case to report, so the
negative case can never match nothing."""
import importlib.util
import unittest
from pathlib import Path

_CI_DIR = Path(__file__).resolve().parents[1] / "reference-plant" / "ci"
_PATH = _CI_DIR / "legs" / "attributed_switch_isolation.py"
_spec = importlib.util.spec_from_file_location(
    "attributed_switch_isolation", _PATH
)
leg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(leg)


class Classification(unittest.TestCase):
    def test_the_attributed_switch_answering_is_the_contract_holding(self):
        self.assertEqual(
            leg.isolation_verdict(True, True),
            "held",
            "the attributed switch answered beside the pinned "
            "submission lane — the control-lane guarantee",
        )

    def test_a_bare_answer_alone_does_not_hold_the_contract(self):
        # The bodiless form is the witness, not the subject: it answers
        # on every release's control lane.
        self.assertEqual(
            leg.isolation_verdict(True, False),
            "held",
            "the attributed switch's own answer is the contract's "
            "subject, whatever the witness did",
        )

    def test_the_attributed_switch_queuing_beside_an_answering_bare_one_is_inconclusive(self):
        self.assertEqual(
            leg.isolation_verdict(False, True),
            "queued-behind",
            "the pre-contract routing quarantined every body-carrying "
            "request: the pinned release predates the contract",
        )

    def test_neither_shape_answering_is_a_failure_not_a_contract_read(self):
        self.assertEqual(
            leg.isolation_verdict(False, False),
            "control-lane-silent",
            "with no control-lane answer in the window the attributed "
            "switch's lateness says nothing about the lane it rode",
        )


class Verdicts(unittest.TestCase):
    def test_an_accepted_switch_reports_its_served_role(self):
        self.assertEqual(
            leg.answered_verdict(200, {"role": "demoting", "tick": 4}),
            "demoting",
        )

    def test_a_refused_switch_reports_its_named_refusal(self):
        self.assertEqual(
            leg.answered_verdict(409, "already_active"), "already_active"
        )
        self.assertEqual(
            leg.answered_verdict(409, {"not_converged": {"sync": {}}}),
            "not_converged",
        )

    def test_an_unexpected_answer_renders_its_status_and_body(self):
        rendered = leg.answered_verdict(503, {"detail": "lane full"})
        self.assertIn("503", rendered)
        self.assertIn("lane full", rendered)


class StagedCongestion(unittest.TestCase):
    def test_every_pinning_request_is_a_severed_scan_batch(self):
        for request in leg.CONGESTION_REQUESTS:
            self.assertTrue(
                request.startswith(b"POST /scan HTTP/1.1\r\n"),
                f"{request!r} is not the submission lane's own path",
            )
            head, _, body = request.partition(b"\r\n\r\n")
            self.assertTrue(
                b"Content-Length:" in head or b"Transfer-Encoding: chunked" in head,
                f"{head!r} declares no body to hold the worker on",
            )
            self.assertLess(
                len(body),
                int(head.split(b"Content-Length: ")[-1])
                if b"Content-Length: " in head
                else len(body) + 1,
                f"{request!r} sent the body its header declared",
            )

    def test_the_set_is_past_the_submission_lane(self):
        # Two workers serve the submission lane; a set that could be
        # served inside the lane's capacity pins nothing and the leg's
        # own witness would refuse the run as inconclusive.
        self.assertGreaterEqual(leg.CONGESTION_CONNECTIONS, 2)
        self.assertEqual(
            len(leg.CONGESTION_SHAPES), len(leg.CONGESTION_REQUESTS),
            "each pinning shape must be named in the digest record",
        )


class DoctoredEvidence(unittest.TestCase):
    def test_a_switch_answering_inside_no_bound_renders_the_declared_evidence(self):
        # The doctored case's declared evidence — what ci/legs.py
        # requires the tampered run's output to carry — must be a
        # substring the leg itself renders, or the `<stem>-unchecked`
        # self-check could never match it.
        evidence = leg.LEG["tampers"][0]["evidence"][0]
        verdict, detail = leg.switch_sample(
            "http://127.0.0.1:1/demote", {"actor": leg.ACTOR}, 0.0
        )
        self.assertIsNone(
            verdict, "a switch on a closed port must answer nothing"
        )
        self.assertIn(evidence, detail)

    def test_the_declared_actor_is_the_qa_attribution_witness(self):
        self.assertTrue(leg.ACTOR.startswith("qa-"))


if __name__ == "__main__":
    unittest.main()