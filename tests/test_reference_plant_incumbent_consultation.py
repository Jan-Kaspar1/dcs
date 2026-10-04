"""The incumbent-consultation leg's decision seams, unit-tested:
`consulted` is the predating-release classification — a relaunched
lifetime that journalled no `restart_consult` ran no consult at all,
which is the pre-contract shape the pinned release reads as rather
than a defect, so the leg reports `inconclusive` with one stable
reason both passes share — and `replay` is the single place the leg's
model (`TAKEOVER`) is read against a half's served facts: the
journaled consult verdict, the grant verdict on that half's named
surface, the resumed baseline the takeover record names, the field
claim's holder, and whether the incumbent's applied receipted state
survived.

These tests pin that discrimination in both directions — the honest
served record of each half replaying clean, and the defect shapes
(a consult that never reached the incumbent's line, a granted claim
over a live one, a baseline naming a state the deployment never
persisted, the incumbent's claim displaced, its receipted tune rolled
back) each reporting — the model naming a distinct surface per half,
the doctor's evidence phrase being a substring ci/legs.py can match,
the leg's registration validating beside the directory's other legs,
and the release contract declaring the three diagnostics the leg
reports."""
import contextlib
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_CI_DIR = _ROOT / "reference-plant" / "ci"
_PATH = _CI_DIR / "legs" / "incumbent_consultation.py"
_spec = importlib.util.spec_from_file_location("incumbent_consultation", _PATH)
leg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(leg)

_PREDATING_REASON = (
    "the pinned release predates the restart-as-active incumbent consult"
)


def consulted_event(outcome):
    """A served `restart_consult` entry carrying `outcome` at seq 9,
    behind a run boundary at seq 8 — the shape a relaunched lifetime's
    served journal carries."""
    return {
        "seq": 9,
        "tick": 7,
        "event": {
            "restart_consult": {
                "source": "127.0.0.1:40001",
                "outcome": outcome,
            }
        },
    }


def refusal_half():
    """The honest served record of the live-incumbent half: the
    consult adopted the reachable incumbent's newer line in place of the
    resumed baseline, the startup grant refused by the named verdict
    with the observed claimant attributed to the incumbent, the
    rejoined run reporting `standby`/`unsynchronized`, the field's
    arbitration still naming the incumbent, and the incumbent's
    receipted tune carried on both images."""
    return {
        "boundary": {"run": 2, "tick": 10, "seq": 8},
        "consult": consulted_event(
            {"adopted": {"superseded_at": 7, "resumed_at": 10}}
        )["event"]["restart_consult"],
        "refusal": {
            "error": {"field_claim_failed": {"detail": "a live peer holds"}}
        },
        "observed": {"point": 100, "claimant": 4242},
        "entries": [
            {"seq": 9, "event": consulted_event({})["event"]},
        ],
        "role": {"role": "standby", "sync": "unsynchronized", "field_claim": "held"},
        "holder": "incumbent",
        "carried": True,
        "carried_detail": None,
        "persisted": 7,
        "incumbent_tick": 10,
    }


def seizure_half():
    """The honest served record of the dead-incumbent half: the
    consult could not be answered and journalled `unadopted`, the
    lifetime's run boundary sits at the resumed baseline, the granted
    run reports `active` with the field claim `held`, and the field's
    arbitration names the restartee's own token."""
    return {
        "boundary": {"run": 3, "tick": 12, "seq": 317},
        "consult": {
            "source": "127.0.0.1:40001",
            "outcome": {
                "unadopted": {
                    "detail": "incumbent checkpoint pull failed: refused"
                }
            },
        },
        "refusal": None,
        "observed": None,
        "entries": [
            {
                "seq": 318,
                "event": {
                    "restart_consult": {
                        "source": "127.0.0.1:40001",
                        "outcome": {
                            "unadopted": {
                                "detail": "incumbent checkpoint pull failed: refused"
                            }
                        },
                    }
                },
            }
        ],
        "role": {"role": "active", "sync": None, "field_claim": "held"},
        "holder": "restartee",
        "carried": True,
        "carried_detail": None,
        "persisted": 12,
        "incumbent_tick": 10,
    }


class Model(unittest.TestCase):
    """The leg's model — stated once, and applied only through
    `replay`."""

    def test_the_model_names_both_halves(self):
        self.assertEqual(
            sorted(leg.TAKEOVER),
            ["refusal-over-live-incumbent", "seizure-over-dead-incumbent"],
        )

    def test_each_half_declares_the_whole_record(self):
        for half, record in leg.TAKEOVER.items():
            for field in (
                "consult",
                "grant_surface",
                "grant",
                "baseline",
                "holder",
                "carried",
            ):
                self.assertIn(field, record, f"{half} omits {field}")

    def test_each_half_reads_its_own_surface(self):
        refusal = leg.TAKEOVER["refusal-over-live-incumbent"]
        seizure = leg.TAKEOVER["seizure-over-dead-incumbent"]
        self.assertEqual(refusal["consult"], "adopted")
        self.assertEqual(seizure["consult"], "unadopted")
        self.assertEqual(refusal["grant"], "field_claim_failed")
        self.assertEqual(seizure["grant"], "held")
        self.assertNotEqual(refusal["grant_surface"], seizure["grant_surface"])
        self.assertNotEqual(refusal["baseline"], seizure["baseline"])

    def test_both_halves_require_the_receipted_state_to_survive(self):
        for half, record in leg.TAKEOVER.items():
            self.assertTrue(record["carried"], half)


class ConsultVerdicts(unittest.TestCase):
    def test_the_three_outcome_variants_read_by_name(self):
        for variant, outcome in (
            ("adopted", {"adopted": {"superseded_at": 7, "resumed_at": 10}}),
            ("standing", {"standing": {"incumbent_at": 7}}),
            ("unadopted", {"unadopted": {"detail": "connect failed"}}),
        ):
            event = {"source": "127.0.0.1:40001", "outcome": outcome}
            self.assertEqual(leg.consult_verdict(event), variant)

    def test_an_absent_or_unreadable_outcome_is_no_verdict(self):
        self.assertIsNone(leg.consult_verdict(None))
        self.assertIsNone(leg.consult_verdict({"source": "127.0.0.1:1"}))
        self.assertIsNone(leg.consult_verdict({"outcome": "adopted"}))


class GrantSurfaces(unittest.TestCase):
    def test_the_refused_grant_names_its_own_refusal_variant(self):
        served = {
            "refusal": {
                "error": {"field_claim_failed": {"detail": "a live peer"}}
            }
        }
        self.assertEqual(
            leg.grant_verdict("startup_claim_refused", served),
            "field_claim_failed",
        )

    def test_the_granted_run_reads_the_field_claim_off_its_role_report(self):
        served = {"role": {"role": "active", "field_claim": "held"}}
        self.assertEqual(leg.grant_verdict("role_field_claim", served), "held")

    def test_an_absent_surface_answers_no_verdict(self):
        self.assertIsNone(leg.grant_verdict("startup_claim_refused", {}))
        self.assertIsNone(leg.grant_verdict("role_field_claim", {}))
        self.assertIsNone(leg.grant_verdict("unknown_surface", {}))


class Baselines(unittest.TestCase):
    def test_the_adopted_consult_names_the_superseded_resumed_tick(self):
        served = {
            "consult": {
                "outcome": {"adopted": {"superseded_at": 7, "resumed_at": 10}}
            }
        }
        self.assertEqual(
            leg.baseline_from("adopted.superseded_at", served), 7
        )

    def test_the_takeover_without_an_adoption_names_its_run_boundary(self):
        served = {"boundary": {"run": 3, "tick": 12, "seq": 317}}
        self.assertEqual(leg.baseline_from("run_boundary", served), 12)

    def test_an_unadopted_consult_names_no_adopted_baseline(self):
        served = {"consult": {"outcome": {"unadopted": {"detail": "refused"}}}}
        self.assertIsNone(leg.baseline_from("adopted.superseded_at", served))


class Replay(unittest.TestCase):
    def test_the_honest_live_incumbent_record_replays_clean(self):
        failures = []
        marks = leg.replay("refusal-over-live-incumbent", refusal_half(), failures)
        self.assertEqual(failures, [])
        self.assertEqual(marks["consult"], "adopted")
        self.assertEqual(marks["grant"], "field_claim_failed")
        self.assertEqual(marks["baseline"], 7)
        self.assertTrue(marks["ordered"])
        self.assertEqual(marks["holder"], "incumbent")
        self.assertTrue(marks["carried"])

    def test_the_honest_dead_incumbent_record_replays_clean(self):
        failures = []
        marks = leg.replay("seizure-over-dead-incumbent", seizure_half(), failures)
        self.assertEqual(failures, [])
        self.assertEqual(marks["consult"], "unadopted")
        self.assertEqual(marks["grant"], "held")
        self.assertEqual(marks["baseline"], 12)
        self.assertTrue(marks["ordered"])
        self.assertEqual(marks["holder"], "restartee")
        self.assertTrue(marks["carried"])

    def test_a_consult_that_never_reached_the_incumbent_is_reported(self):
        served = refusal_half()
        served["consult"] = {
            "source": "127.0.0.1:40001",
            "outcome": {"unadopted": {"detail": "pull failed"}},
        }
        failures = []
        marks = leg.replay("refusal-over-live-incumbent", served, failures)
        self.assertEqual(marks["consult"], "unadopted")
        self.assertTrue(any("journalled the 'unadopted' verdict" in f for f in failures))

    def test_a_baseline_naming_a_state_the_deployment_never_persisted_is_reported(self):
        served = seizure_half()
        served["persisted"] = 11
        failures = []
        leg.replay("seizure-over-dead-incumbent", served, failures)
        self.assertTrue(
            any("resumed baseline reads 12" in failure for failure in failures),
            failures,
        )

    def test_a_ledger_journalled_ahead_of_the_run_boundary_is_reported(self):
        served = seizure_half()
        served["boundary"] = {"run": 3, "tick": 12, "seq": 400}
        failures = []
        marks = leg.replay("seizure-over-dead-incumbent", served, failures)
        self.assertFalse(marks["ordered"])
        self.assertTrue(any("consultation ledger" in f for f in failures))

    def test_a_claim_granted_over_a_live_incumbent_is_reported(self):
        # The defect shape: the grant was granted, so no refused-grant
        # entry was journalled at all — the restartee seized the field.
        served = refusal_half()
        served["refusal"] = None
        failures = []
        marks = leg.replay("refusal-over-live-incumbent", served, failures)
        self.assertIsNone(marks["grant"])
        self.assertTrue(
            any("startup grant answered None" in f for f in failures), failures
        )

    def test_the_incumbents_claim_displaced_is_reported(self):
        served = refusal_half()
        served["holder"] = "restartee"
        failures = []
        leg.replay("refusal-over-live-incumbent", served, failures)
        self.assertTrue(
            any("names the 'restartee' claim" in f for f in failures), failures
        )

    def test_a_rolled_back_receipted_tune_is_reported(self):
        served = seizure_half()
        served["carried"] = False
        served["carried_detail"] = "the parameter report reads {'float': 1.0}"
        failures = []
        marks = leg.replay("seizure-over-dead-incumbent", served, failures)
        self.assertFalse(marks["carried"])
        self.assertTrue(
            any("{'float': 1.0}" in f for f in failures), failures
        )


class LifetimeReadings(unittest.TestCase):
    def test_the_most_recent_lifetime_is_the_launch_the_leg_staged(self):
        entries = [
            {"seq": 1, "event": {"run_boundary": {"run": 1}}},
            {"seq": 2, "event": {"point_changed": {}}},
            {"seq": 3, "event": {"run_boundary": {"run": 2}}, "tick": 10},
            consulted_event({"adopted": {"superseded_at": 7, "resumed_at": 10}}),
        ]
        life = leg.latest_lifetime(entries)
        self.assertEqual(life["run"], 2)
        self.assertEqual(life["tick"], 10)
        self.assertEqual(life["seq"], 3)
        self.assertEqual(len(life["entries"]), 1)

    def test_a_journal_with_no_boundary_reads_as_no_lifetime(self):
        life = leg.latest_lifetime([{"seq": 1, "event": {}}])
        self.assertIsNone(life["run"])
        self.assertEqual(life["entries"], [])

    def test_the_sync_verdict_reads_as_one_word(self):
        self.assertEqual(
            leg.sync_word({"sync": {"tracking": {"aligned": 3}}}), "tracking"
        )
        self.assertEqual(leg.sync_word({"sync": "unsynchronized"}), "unsynchronized")
        self.assertIsNone(leg.sync_word({"role": "active", "sync": None}))

    def test_the_resumed_tick_reads_off_the_launch_preamble(self):
        self.assertEqual(
            leg.resumed_tick(["resumed from state file /x/state.json at tick 7"]),
            7,
        )
        self.assertIsNone(leg.resumed_tick(["listening on 127.0.0.1:1"]))

    def test_a_minted_token_is_named_not_carried(self):
        self.assertEqual(leg.holder_word(7, {7: "incumbent"}), "incumbent")
        self.assertEqual(leg.holder_word(9, {7: "incumbent"}), "foreign")
        self.assertEqual(leg.holder_word(None, {7: "incumbent"}), "unheld")


class PredatingRelease(unittest.TestCase):
    """The classification that keeps a predating pinned release an
    inconclusive reading rather than a product failure — and the one
    stable reason both passes must share."""

    def test_a_journalled_consult_is_returned(self):
        entries = [consulted_event({"adopted": {"superseded_at": 7, "resumed_at": 10}})]
        self.assertEqual(
            leg.consulted(entries, "the live-incumbent restart's")["source"],
            "127.0.0.1:40001",
        )

    def test_a_lifetime_with_no_consult_is_the_predating_shape(self):
        entries = [
            {"seq": 9, "event": {"startup_claim_refused": {"error": {}}}},
            {"seq": 10, "event": {"role_changed": {"from": "active", "to": "standby"}}},
        ]
        with self.assertRaises(leg.Inconclusive) as raised:
            leg.consulted(entries, "the live-incumbent restart's")
        self.assertEqual(raised.exception.args[0], _PREDATING_REASON)
        self.assertIn("no restart_consult entry", raised.exception.args[-1])

    def test_both_halves_classify_with_the_same_stable_reason(self):
        reasons = set()
        for half in ("the live-incumbent restart's", "the dead-incumbent takeover's"):
            with self.assertRaises(leg.Inconclusive) as raised:
                leg.consulted([], half)
            reasons.add(raised.exception.args[0])
        self.assertEqual(reasons, {_PREDATING_REASON})


class InconclusiveReport(unittest.TestCase):
    """`main`'s inconclusive report: the digest line carries only the
    stable reason, so two identical passes agree, and the doctored
    case still fails naming its evidence — an inconclusive run offers
    it none."""

    def run_main(self, tamper):
        out, err = io.StringIO(), io.StringIO()
        original = leg.incumbent_consultation_pass
        leg.incumbent_consultation_pass = lambda args, chosen: (_ for _ in ()).throw(
            leg.Inconclusive(_PREDATING_REASON, "the lifetime journalled none")
        )
        # Only the scenario document is read before the pass runs, and
        # the pass itself is stubbed to classify inconclusive.
        argv = [
            "incumbent_consultation.py",
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
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                status = leg.main()
        finally:
            sys.argv = saved
            leg.incumbent_consultation_pass = original
        return status, out.getvalue(), err.getvalue()

    def test_the_digest_line_carries_only_the_stable_reason(self):
        status, out, _err = self.run_main(None)
        self.assertEqual(status, 0)
        self.assertEqual(
            out.strip(),
            f"incumbent-consultation-digest inconclusive — {_PREDATING_REASON}",
        )

    def test_the_doctored_case_fails_naming_its_evidence(self):
        status, _out, err = self.run_main("expect-seized")
        self.assertEqual(status, 1)
        self.assertIn(leg.LEG["tampers"][0]["evidence"][0], err)


class Registration(unittest.TestCase):
    def test_the_leg_registers_a_free_order_with_a_doctored_case(self):
        self.assertEqual(leg.LEG["order"], 830)
        self.assertEqual(leg.LEG["passes"], "incumbent-consultation-leg")
        self.assertEqual(len(leg.LEG["tampers"]), 1)

    def test_the_leg_validates_beside_the_directorys_others(self):
        legs = importlib.util.spec_from_file_location(
            "legs", _CI_DIR / "legs.py"
        )
        driver = importlib.util.module_from_spec(legs)
        legs.loader.exec_module(driver)
        discovered = driver.discover(str(_CI_DIR / "legs"))
        orders = [found["order"] for found in discovered]
        self.assertEqual(len(orders), len(set(orders)), "two legs share an order")
        stems = [found["stem"] for found in discovered]
        self.assertIn("incumbent-consultation", stems)

    def test_the_declared_evidence_is_a_phrase_the_leg_renders(self):
        evidence = leg.LEG["tampers"][0]["evidence"][0]
        self.assertIn(evidence, leg.TAMPER_EVIDENCE)
        self.assertIn(evidence, leg.LEG["tampers"][0]["passed"] + leg.LEG["tampers"][0]["missed"] + evidence)

    def test_the_actor_is_the_leg_own_attribution(self):
        self.assertTrue(leg.ACTOR.startswith("ci-"))

    def test_the_contract_declares_the_emitted_diagnostics(self):
        contract = (_ROOT / "docs" / "release-contract.md").read_text()
        for name in (
            "`incumbent-consultation-failed`",
            "`incumbent-consultation-nondeterministic`",
            "`incumbent-consultation-unchecked`",
        ):
            self.assertIn(name, contract)

    def test_the_leg_references_no_platform_checkout_path(self):
        source = _PATH.read_text()
        for leak in ("crates/", "../", "file://", "/home/", "target/debug"):
            self.assertNotIn(leak, source, f"the leg names {leak}")


if __name__ == "__main__":
    unittest.main()