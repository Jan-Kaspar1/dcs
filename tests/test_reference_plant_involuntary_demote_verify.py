"""The involuntary_demote_verify leg's unit coverage — ci/legs/
involuntary_demote_verify.py is the reference plant's consumer-boundary
mirror of the rig's involuntary-demote-verify exercise (decision 92's
contract, landed as the announced-source verify): a field owner demoted
by a fenced write after its claim was preempted crosses no request
boundary, so its recorded `?peer=` announced set is consumed only
through lazy verification — a recorded hint is a verify candidate, never
a pull target, a bare hint on an unkeyed run is no tracking source at
all, and a verified successor's endpoint pins with a journaled
`tracking_source_adopted`. These tests pin, without launching the pair:
the leg's registration record, its demote-in-place drive and
launch-role restore against scripted peers, the durable-journal
projections the audit reads, and the inconclusive and doctored-case
classifications the harness relies on."""
import contextlib
import importlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
_CI_DIR = _ROOT / "reference-plant" / "ci"
_LEGS_DIR = _CI_DIR / "legs"
_LEG_PATH = _LEGS_DIR / "involuntary_demote_verify.py"
_SCENARIO_PATH = _CI_DIR / "scenario.json"
_MANIFEST_PATH = _ROOT / "reference-plant" / "deploy" / "manifest.json"

DUTY = "http://127.0.0.1:9001"
PEER = "http://127.0.0.1:9002"


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
leg = load(_LEG_PATH, "involuntary_demote_verify")


def argv(tamper=None):
    args = [
        "involuntary_demote_verify.py",
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
            mock.patch.object(leg, "involuntary_demote_verify_pass", stub), \
            contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        rc = leg.main()
    return rc, out.getvalue(), err.getvalue()


def journal_file(events):
    path = Path(tempfile.mkdtemp()) / "journal.jsonl"
    with open(path, "w") as handle:
        handle.write(json.dumps({"run_boundary": {"run": 1, "tick": 0}}) + "\n")
        for index, event in enumerate(events, 1):
            handle.write(
                json.dumps({"entry": {"seq": index, "tick": index,
                                      "event": event}}) + "\n"
            )
    return path


def role(name, sync=None):
    report = {"role": name}
    if sync is not None:
        report["sync"] = sync
    return report


def scripted(reports, default=None):
    """A `pair.get`/`pair.scan` stand-in: `reports` maps a monitor base
    url to `(sequence, default)` — the RoleReports its successive
    `GET /role` reads answer, and the report it answers once the
    sequence is spent. A bare sequence takes the global `default`. The
    driven url list is returned so the watch's own cadence can be
    asserted."""
    seen = []
    cursor = {base: 0 for base in reports}

    def get(url, what, failures):
        base = url.rsplit("/", 1)[0]
        index = cursor.get(base, 0)
        cursor[base] = index + 1
        entry = reports.get(base)
        if entry is None:
            return default
        sequence, fallback = (
            entry if isinstance(entry, tuple) else (entry, default)
        )
        return sequence[index] if index < len(sequence) else fallback

    def scan(url, failures):
        seen.append(url)

    return mock.patch.multiple(leg.pair, get=get, scan=scan), seen


class Registration(unittest.TestCase):
    def test_the_leg_registers_in_the_pair_stage(self):
        discovered = {
            Path(record["file"]).name: record
            for record in legs.discover(str(_LEGS_DIR))
        }
        record = discovered["involuntary_demote_verify.py"]
        self.assertEqual(record["stem"], "involuntary-demote-verify")
        self.assertEqual(record["order"], 205)
        self.assertEqual(record["passes"], "involuntary-demote-verify")

    def test_the_announced_hint_legs_share_the_hostile_endpoint(self):
        # The staging is imported, not restated: the pull ledger the
        # announced-source-verify leg records is the ledger this leg
        # asserts its bounded verify pass against. Read the shared module
        # through the leg's own reference — a sibling test module's
        # reload replaces the name in `sys.modules`, and identity
        # against the reloaded copy would prove nothing about sharing.
        shared = leg.announced_source_verify
        self.assertTrue(leg.ForeignEndpoint is shared.ForeignEndpoint)
        self.assertTrue(leg.refusals is shared.refusals)
        self.assertIn("import announced_source_verify",
                      normalized_source(_LEG_PATH))

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
            "`involuntary-demote-verify-failed`",
            "`involuntary-demote-verify-nondeterministic`",
            "`involuntary-demote-verify-unchecked`",
        ):
            self.assertIn(name, contract)


class VerdictClassification(unittest.TestCase):
    EVIDENCE = {
        "converged": 12,
        "announced_at": 12,
        "keyed": {"rounds": 2, "audit": {"verify_pulls": 1}},
        "unkeyed": {"rounds": 2, "foreign_pulls": 0},
        "restored": 40,
    }

    def test_a_clean_pass_prints_the_digest_line(self):
        rc, out, err = run_main(
            outcome=([{"phase": "involuntary"}], self.EVIDENCE, []))
        self.assertEqual(rc, 0)
        self.assertIn("involuntary-demote-verify-digest ", out)
        self.assertIn("1 verify pull", out)
        self.assertEqual(err, "")

    def test_a_contract_violation_exits_nonzero_by_name(self):
        rc, out, err = run_main(
            outcome=([], self.EVIDENCE, ["the bare hint was adopted"]))
        self.assertEqual(rc, 1)
        self.assertNotIn("involuntary-demote-verify-digest ", out)
        self.assertIn(
            "involuntary-demote-verify: the bare hint was adopted", err
        )

    def test_a_predating_release_reports_inconclusive_and_exits_zero(self):
        rc, out, err = run_main(
            outcome=leg.Inconclusive("no claim staging lever"))
        self.assertEqual(rc, 0)
        self.assertEqual(
            out.strip(),
            "involuntary-demote-verify-digest inconclusive — "
            "no claim staging lever",
        )
        self.assertIn("inconclusive", err)

    def test_the_inconclusive_marker_is_identical_across_passes(self):
        # The pair stage compares the two passes' stdout — the marker's
        # stable phrase carries no run's own verdicts or endpoints.
        first = run_main(outcome=leg.Inconclusive("the contract is off"))[1]
        second = run_main(outcome=leg.Inconclusive("the contract is off"))[1]
        self.assertEqual(first, second)

    def test_a_doctored_case_offers_an_inconclusive_run_no_evidence(self):
        rc, out, err = run_main(
            tamper="expect-adopted",
            outcome=leg.Inconclusive("the contract is off"),
        )
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("offers the doctored case no evidence", err)

    def test_a_doctored_case_that_satisfies_the_doctor_exits_nonzero(self):
        rc, out, err = run_main(
            tamper="expect-adopted", outcome=([], {}, []))
        self.assertEqual(rc, 1)
        self.assertIn("passed silently", err)


class JournalProjections(unittest.TestCase):
    """The durable half of an involuntary demotion: the fenced role walk
    the supersede owes, the attributed claim loss, and the refused
    probe. A `role_changed` walk an operator request produced carries no
    `origin: "fenced"` — which is exactly how a request-path demotion
    would slip through an audit that only counted transitions."""

    EVENTS = [
        {"role_changed": {"from": "active", "to": "demoting",
                          "origin": "fenced"}},
        {"field_claim_lost": {"point": 100, "claimant": 424249}},
        {"role_changed": {"from": "demoting", "to": "standby",
                          "origin": "fenced"}},
        {"tracking_source_refused": {
            "source": "127.0.0.1:8090",
            "detail": "the pull's line proof did not verify under the pair key"}},
        {"tracking_source_adopted": {"source": "127.0.0.1:9002"}},
    ]

    def test_the_fenced_walk_is_the_involuntary_path(self):
        path = journal_file(self.EVENTS)
        self.assertEqual(
            leg.fenced_walk(path, 0),
            [("active", "demoting", "fenced"),
             ("demoting", "standby", "fenced")],
        )

    def test_a_request_path_walk_is_not_the_fenced_one(self):
        path = journal_file([
            {"role_changed": {"from": "active", "to": "demoting",
                              "origin": "request"}},
            {"role_changed": {"from": "demoting", "to": "standby",
                              "origin": "request"}},
        ])
        self.assertEqual(
            leg.fenced_walk(path, 0),
            [("active", "demoting", "request"),
             ("demoting", "standby", "request")],
        )
        self.assertNotIn(("active", "demoting", "fenced"),
                         leg.fenced_walk(path, 0))

    def test_the_claim_loss_is_read_with_its_attribution(self):
        path = journal_file(self.EVENTS)
        losses = leg.claim_losses(path, 0)
        self.assertEqual(len(losses), 1)
        self.assertEqual(losses[0]["claimant"], 424249)

    def test_every_floor_after_the_boundary_is_the_episodes_own(self):
        path = journal_file(self.EVENTS)
        # Records are the cold-start boundary, then one per event — a
        # floor of one leaves the episode's own records only.
        self.assertEqual(len(leg.claim_losses(path, 1)), 1)
        self.assertEqual(leg.claim_losses(path, 6), [])
        self.assertEqual(
            [detail for _s, detail in leg.refusals(path, 1)],
            ["the pull's line proof did not verify under the pair key"],
        )

    def test_a_file_with_no_episode_reads_empty(self):
        path = journal_file([{"point_changed": {"point": 1}}])
        self.assertEqual(leg.fenced_walk(path, 0), [])
        self.assertEqual(leg.claim_losses(path, 0), [])
        self.assertEqual(leg.journal_kinds(path, 0),
                         [("point_changed", {"point": 1})])


class Drive(unittest.TestCase):
    """`drive` is the demote-in-place and re-join watch: one driven scan
    per launched peer per round until the episode's accept condition
    holds. A claim preemption lands its demotion on the superseded
    owner's own fenced write, so the watch is what drives it — an
    undriven round would never demote anything."""

    def test_it_drives_every_peer_each_round_until_the_condition_holds(self):
        reports = {
            DUTY: [role("active"), role("standby", {"tracking": {}})],
            PEER: [role("standby", {"tracking": {}}), role("active")],
        }
        patcher, seen = scripted(reports)
        with patcher:
            settled = leg.drive(
                [DUTY, PEER], 8,
                lambda roles: leg.role_of(roles[1]) == "active"
                and leg.role_of(roles[0]) == "standby",
                [], "the demotion",
            )
        self.assertEqual(settled["rounds"], 2)
        self.assertEqual(seen, [DUTY, PEER, DUTY, PEER])
        self.assertEqual(len(settled["roles"]), 2)

    def test_a_condition_that_never_holds_is_reported_by_name(self):
        patcher, seen = scripted({}, default=role("active"))
        failures = []
        with patcher:
            settled = leg.drive(
                [DUTY, PEER], 3, lambda roles: False, failures,
                "the demotion",
            )
        self.assertIsNone(settled)
        self.assertEqual(len(seen), 6)
        self.assertIn("the demotion never settled within 3 driven rounds",
                      failures[0])


class RestoreRoles(unittest.TestCase):
    """The launch-role walk back. A demotion releases the field's
    single-writer claim, which the declared duty controller's
    conditional claim then re-seats for itself — so the walk promotes
    the duty controller only when it did not already re-own the field,
    and never issues a promote against a peer already `active`."""

    def test_a_pair_already_on_its_launch_roles_is_left_alone(self):
        patcher, _ = scripted(
            {DUTY: [role("active")],
             PEER: [role("standby", {"tracking": {}})]},
            default=role("standby", {"tracking": {}}),
        )
        with patcher, mock.patch.object(leg.pair, "request") as post:
            failures = []
            leg.restore_roles(DUTY, PEER, failures, 4, "the restore")
        self.assertEqual(failures, [])
        post.assert_not_called()

    def test_a_peer_holding_the_field_is_demoted_and_the_field_left_free(self):
        # The demotion hands the field back to the conditional claimant,
        # so the walk promotes only when the duty controller did not
        # re-own it — and the request sequence shows it demoting first.
        reports = {
            DUTY: ([role("standby", {"tracking": {}})],
                   role("active")),
            PEER: ([role("active")], role("standby", {"tracking": {}})),
        }
        patcher, _ = scripted(reports)
        with patcher, mock.patch.object(
            leg.pair, "request", return_value=(200, {})
        ) as post:
            failures = []
            leg.restore_roles(DUTY, PEER, failures, 6, "the restore")
        self.assertEqual(failures, [])
        self.assertEqual(
            [call.args[0] for call in post.call_args_list],
            [f"{PEER}/demote"],
        )

    def test_a_pair_the_walk_cannot_settle_is_reported(self):
        patcher, _ = scripted({}, default=role("standby"))
        with patcher, mock.patch.object(
            leg.pair, "request", return_value=(409, "already_active")
        ):
            failures = []
            leg.restore_roles(DUTY, PEER, failures, 2, "the restore")
        self.assertTrue(
            any("could not be walked back onto its launch roles" in line
                for line in failures),
            failures,
        )


class RoleVocabulary(unittest.TestCase):
    def test_a_converged_verdict_is_tracking_or_orphaned(self):
        self.assertEqual(leg.CONVERGED_VERDICTS, ("tracking", "orphaned"))
        # `degraded` is the wedge this contract exists to refuse — a
        # demoted peer stranded on the endpoint it followed.
        self.assertNotIn("degraded", leg.CONVERGED_VERDICTS)

    def test_the_unkeyed_budget_is_one_short_of_a_verify_pass(self):
        # A bare hint on an unkeyed run earns not even a bounded verify
        # pull, so the allowance the leg asserts against is zero.
        self.assertEqual(leg.UNKEYED_PULL_BOUND, 1)

    def test_only_the_switch_own_answers_are_a_preempt_verdict(self):
        # The switch's own 200 and its named 409 refusals are product
        # verdicts — a refused preempt is a failure the leg reports.
        # Anything else (an unrouted 404 on a release predating the
        # verb) admits no claim-preempt staging at all, which is
        # inconclusive, never a product failure.
        self.assertEqual(leg.PREEMPT_ANSWERS, (200, 409))

    def test_role_of_tolerates_a_dropped_report(self):
        self.assertEqual(leg.role_of({"role": "active"}), "active")
        self.assertIsNone(leg.role_of(None))


if __name__ == "__main__":
    unittest.main()
