#!/usr/bin/env python3
"""The adopted-receipt-regression leg for the reference plant — the
consumer-side proof that an adopted checkpoint's staler receipt view
cannot regress a locally terminal outcome, so a covering adoption
never re-settles an admission and journals a second identical
`command_settled` for it (WW-ENG-003, WW-LCM-001 — the contract
#709's fix establishes, mirrored at the customer boundary from the
rig's adopted-receipt-regression leg).

The contract: `adopt_receipts` is forward-only inside the covered
stretch. An adopted terminal outcome landing over a local `Accepted`
is the ordinary forward settle; an adopted `Accepted` over a local
terminal outcome does not land, because the document's view at that
index is staler than the run's own record. With no terminal-to-pending
regression there is no second settle for the covering adoption to
emit, and the pair's command audit carries exactly one terminal settle
per admission. The defect the finding recorded: the merge clone_from'd
the adopted window verbatim, so a peer's staler view moved a settled
receipt back to `accepted` silently, and the next covering adoption
restored it — a replay of the already-recorded verdict that the
recorder read as a fresh transition and journaled a second time, on
both peers, because the pair syncs the journal.

The declared pair is driven — every pull a peer performs happens
inside its own `POST /scan` — so which peer is scanned when is the
whole staging, with no freeze and no timing race. The run:

- converges the declared pair to its launch roles — the manifest's
  field owner `active`, the standby `tracking` — through the pair
  rig's driven-tick loop, and gates the contract surface: each peer's
  served checkpoint must carry the receipt window and admission
  counters the audit correlates by, each receipt must carry the
  declared `actor`/`reason`, and the manifest must declare the journal
  files the durable half reads. A pinned release predating that
  surface reports `receipt-regression-digest inconclusive`, never a
  failure;
- records the admission's absolute submission index and submits a
  receipted `write_value` on the declared writable point;
- scans the *standby* and only the standby, so its pull carries the
  still-pending admission — its served window now shows the index
  `accepted`, the staler view the contract is about — while the
  owner's own boundary has not run yet;
- scans the owner: its field-owning boundary applies the admission
  and journals the settle, so the owner holds the terminal verdict;
- demotes the owner and scans it: its tracking pull adopts the
  standby's window, whose view of that index is the staler `accepted`
  — the regression the contract refuses. The owner's receipt must
  still read the terminal verdict, the admission must not be
  re-queued, and the field must not see a second application;
- scans the standby to close the window — the covering adoption that
  confirms the verdict — and audits through both peers' serving
  monitors and both declared durable journals: each carries exactly
  one `command_settled` for the admission and each keeps serving the
  terminal verdict;
- promotes the manifest's field owner back and audits the pair
  reconverging `active` plus `tracking` — the pair ends on its launch
  roles.

The contract postdates the release line's v0.3.0 cut: where the
launched tooling predates it the run's own evidence is the
pre-contract shape — a checkpoint carrying no receipt window or
admission counters, a receipt dropping its declared `actor`/`reason`,
the standby's pull never carrying the pending admission, or the
demote dropping the admission instead of holding it — and the leg
reports `receipt-regression-digest inconclusive` rather than
asserting until the manifest repins a release carrying the contract.

Usage:

    adopted_receipt_regression.py --plant-server PATH \
        --controller PATH --model model/plant.json \
        --dynamics model/dynamics.json --scenario ci/scenario.json \
        --manifest deploy/manifest.json

On success one `receipt-regression-digest <sha256>` line prints — the
check runs two passes and compares them
(`receipt-regression-nondeterministic`). A contract violation reports
`receipt-regression: …` lines on stderr and exits 1 — the check's
`receipt-regression-failed`. `--tamper expect-regressed` doctors the
leg's own expectation to the defect shape — accepting a served
`accepted` for an admission whose settle already journaled — so the
leg proves its regression audit fires on the honest terminal record
rather than passing an unexercised contract.
"""

import argparse
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import claim_reclaim
import force_carryover
import pair
import simulate
import takeover


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its two
# digest-identical passes, and exercise its doctored cases. The
# doctored case: the leg accepting a served `accepted` for an
# admission whose settle already journaled — the defect the contract
# closed — must surface the named diagnostic on the honest terminal
# record.
LEG = {
    "order": 605,
    "title": "the adopted-receipt-regression leg",
    "passes": "adopted-receipt-regression",
    "tampers": [
        {
            "name": "expect-regressed",
            "passed": "an expect-regressed case passed the adopted-receipt-regression leg",
            "missed": "the expect-regressed case did not report its named diagnostic",
            "evidence": ["the doctored expectation accepted a regressed receipt"],
        },
    ],
}


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The pinned release predates — or never covers — the contract
    the leg exercises: the run classifies inconclusive, never a
    product failure."""


# The driven-scan bounds each phase runs: the promoted-owner's settle,
# the fenced demote's detection scans, the standby's covering pull,
# the owner's own adoption of the staler view, the pair's
# reconvergence, and the launch-role hold train.
FETCH_SETTLE_S = 0.3
WATCH_SCANS = 8
WINDOW_SCANS = 8
RESTORE_TICKS = 4

# The admission's declared identity — unique in both peers' receipt
# logs, the (command, actor) pair the audit correlates by.
ACTOR = "ci-receipt-regression"
REASON = "adopted-receipt-regression"


def verdict(receipt):
    """A receipt's normalized verdict — the outcome name beside the
    apply tick. The tick rides it because the finding's duplicate was
    a replay of an already-recorded `applied{tick}` verdict: a
    serving log answering only `applied` cannot tell a re-journaled
    replay from a second settlement."""
    outcome = simulate.receipt_outcome(receipt)
    applied = (receipt.get("outcome", {}).get("applied") or {}) if \
        isinstance(receipt, dict) else {}
    tick = applied.get("tick")
    return f"{outcome}@{tick}" if isinstance(tick, int) else outcome


def admission_hit(receipt, command, actor):
    """Whether a served or journaled receipt is the named admission —
    the (command, actor) pair is unique to its submission."""
    return (
        isinstance(receipt, dict)
        and receipt.get("command") == command
        and receipt.get("actor") == actor
    )


def admission_receipts(receipts, command, actor):
    """The admission's records in a served receipt log — newest last."""
    return [
        receipt for receipt in receipts
        if admission_hit(receipt, command, actor)
    ]


def served(url, what, failures):
    """One peer's served receipt log."""
    return pair.get(f"{url}/receipts", what, failures)


def journal_entries(path):
    """A `--journal-file`'s entry records in file order — the durable
    half of the audit. The shared reader also returns the run-boundary
    markers; those are lifetimes, not entries."""
    return [
        record
        for kind, record in pair.journal_records(path)
        if kind == "entry"
    ]


def settle_rows(entries, command, actor):
    """`(seq, tick, outcome)` per `command_settled` record answering
    the admission — the normalized settle row the digest compares and
    the audit bounds."""
    rows = []
    for entry in entries:
        event = entry.get("event") or {}
        receipt = (event.get("command_settled") or {}).get("receipt")
        if receipt is None or not admission_hit(receipt, command, actor):
            continue
        rows.append(
            (
                entry.get("seq"),
                entry.get("tick"),
                simulate.receipt_outcome(receipt),
            )
        )
    return rows


def receipt_window(url, what, failures):
    """The retained receipt tail and the absolute submission index of
    its first entry — the (log, high-water) pair the audit correlates
    absolute submission indices by. The surface gate: a window or its
    admission counters absent is the release predating the contract,
    never a violation of it."""
    checkpoint = pair.get(f"{url}/checkpoint", what, failures)
    receipts = checkpoint.get("receipts")
    attempts = (checkpoint.get("command_admission") or {}).get("attempts")
    if not isinstance(receipts, list):
        raise Inconclusive(
            f"{what} carries no receipt window — the pinned release "
            "predates the adopted-receipt-regression contract"
        )
    if not isinstance(attempts, int):
        raise Inconclusive(
            f"{what} carries no admission counters — the pinned "
            "release predates the adopted-receipt-regression contract"
        )
    return checkpoint, receipts, max(0, attempts - len(receipts))


def index_receipt(url, index, what, failures):
    """The served receipt at absolute submission `index`, or None
    while the retained window does not cover it."""
    _checkpoint, receipts, base = receipt_window(url, what, failures)
    position = index - base
    if 0 <= position < len(receipts):
        return receipts[position]
    return None


def next_receipt_index(url, what, failures):
    """The absolute submission index the peer's next `POST /command`
    mints — the window's high-water `base + len(receipts)`."""
    _checkpoint, receipts, base = receipt_window(url, what, failures)
    return base + len(receipts)


def regression_fault(observed, tamper):
    """The named fault the adoption window's observations carry, or
    None when the pair holds the terminal verdict the settle
    journaled: `receipt-regression-failed` while a peer that recorded
    the settle serves no terminal verdict for it at all, and
    `receipt-regression-nondeterministic` while a peer's served
    receipt moves back to the pending view the finding recorded, or
    while the two peers disagree on the verdict. Under the leg's own
    doctored case the pending view is the *accepted* observation, so
    the case surfaces the named diagnostic on the honest record."""
    if tamper == "expect-regressed":
        return (
            "receipt-regression-failed",
            "the doctored expectation accepted a regressed receipt: "
            f"the adoption window served {observed}",
        )
    if not observed:
        return (
            "receipt-regression-failed",
            "the adoption window carried no observation — neither "
            "peer's served receipts ever read",
        )
    for name, reads in observed.items():
        terminal = [
            value for value in reads
            if value and not str(value).startswith("accepted")
        ]
        if not terminal:
            return (
                "receipt-regression-failed",
                f"{name} served no terminal verdict for the "
                f"admission across the adoption window — reads {reads}",
            )
        settled_from = reads.index(terminal[0])
        if any(value and str(value).startswith("accepted")
               for value in reads[settled_from:]):
            return (
                "receipt-regression-nondeterministic",
                f"{name}'s served receipt moved back to the pending "
                f"view after its settle journaled — reads {reads}",
            )
        if len(set(terminal)) > 1:
            return (
                "receipt-regression-nondeterministic",
                f"{name}'s served receipt changed verdict across the "
                f"adoption window — reads {reads}",
            )
    finals = {
        name: [value for value in reads
               if value and not str(value).startswith("accepted")][-1]
        for name, reads in observed.items()
    }
    if len(set(finals.values())) > 1:
        return (
            "receipt-regression-nondeterministic",
            f"the peers' served receipts disagree on the "
            f"admission's verdict — {finals}",
        )
    return None


def pre_contract_shape(observed):
    """Whether every verdict the adoption window observed is a pending
    view — the documented last-pull-wins shape the pinned release
    predating the contract produces (the settle's outcome regressed to
    pending on the peer that journaled it). Any other wrong shape is
    a different defect and stays a product failure."""
    return all(
        not value or str(value).startswith("accepted")
        for reads in observed.values()
        for value in reads
    )


def regression_pass(args, tamper):
    """The adopted-receipt-regression run: converge, stage the staler
    receipt view the finding records — the standby's pull carrying
    the pending admission while the owner's own boundary has not run —
    settle the admission on the owner, demote it into an adoption of
    that staler view, close the window with the covering pull, audit
    both monitors and both durable journals for the single settle,
    and restore the launch roles. Returns `(digest_entries, evidence,
    failures)`; raises `Inconclusive` where the pinned release
    predates the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "adopted-receipt-regression leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    duty_name, standby_name = duty_decl["name"], standby_decl["name"]
    with open(args.model) as handle:
        model = json.load(handle)
    point = force_carryover.force_target(model)
    if point is None:
        raise Abort(
            f"the emitted model declares no writable In point at "
            f"{force_carryover.FORCE_SIGNAL} — the "
            "adopted-receipt-regression leg has nothing to admit"
        )

    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        peers = {duty_name: duty_url, standby_name: standby_url}

        # Phase 1 — convergence: the pair rig's driven-tick loop, the
        # tracking peer scanned first so each pull applies the owner's
        # latest checkpoint and the peers rest at the same tick with
        # identical images.
        converged = rig.converge(failures)
        evidence["converged"] = converged["ticks"][-1]
        digest_entries.append(
            {
                "phase": "converge",
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )

        # The contract surface: both peers' checkpoints must carry the
        # receipt window and admission counters the index correlation
        # reads, and the manifest must declare the journal files the
        # durable half reads.
        for name, url in peers.items():
            receipt_window(url, f"GET /checkpoint on {name}", failures)
        if (
            rig.duty_files.get("journal_file") is None
            or rig.standby_files.get("journal_file") is None
        ):
            raise Inconclusive(
                "the manifest's pair declares no journal_file "
                "persistence — the durable half of the regression "
                "audit is absent"
            )

        baseline = simulate.snapshot_point(converged["owner"], point)
        if baseline is None or "bool" not in baseline:
            failures.append(
                f"the admission's target point {point} serves "
                f"{baseline} — a bool baseline the leg can flip is "
                "required"
            )
            raise Abort
        value = not baseline["bool"]
        command = takeover.write_value(point, value)
        digest_entries.append(
            {
                "phase": "gate",
                "point": point,
                "baseline": baseline,
                "command": command,
            }
        )

        # Phase 2 — the admission: the receipted write mints on the
        # field owner, whose own boundary has not run yet.
        index = next_receipt_index(duty_url, "GET /checkpoint", failures)
        status, receipt = pair.request(
            f"{duty_url}/command",
            {"command": command, "actor": ACTOR, "reason": REASON},
        )
        evidence["submission"] = {"status": status, "receipt": receipt}
        if status != 200 or simulate.receipt_outcome(receipt) != "accepted":
            failures.append(
                f"the admission answered {status} {receipt}, expected "
                "an accepted receipt"
            )
            raise Abort
        if receipt.get("actor") != ACTOR or receipt.get("reason") != REASON:
            raise Inconclusive(
                "the served receipt drops the declared actor/reason — "
                "the submission-record identity the audit correlates "
                "by; the pinned release predates the "
                "adopted-receipt-regression contract"
            )
        digest_entries.append(
            {"phase": "admit", "index": index, "receipt": receipt}
        )

        # Phase 3 — the staler view: only the standby is scanned, so
        # its pull carries the still-pending admission into its own
        # window while the owner's boundary never ran. That served
        # `accepted` view of the admission's own index is the document
        # the owner's later adoption will pull.
        pair.scan(standby_url, failures)
        time.sleep(FETCH_SETTLE_S)
        carried = admission_receipts(
            served(standby_url, "GET /receipts", failures),
            command, ACTOR,
        )
        evidence["stale_view"] = verdict(carried[-1]) if carried else None
        if not carried:
            raise Inconclusive(
                "the standby's pull never carried the pending "
                "admission into its own window — the staler view the "
                "contract is about never formed; the pinned release "
                "predates the adopted-receipt-regression contract"
            )
        if simulate.receipt_outcome(carried[-1]) != "accepted":
            raise Inconclusive(
                "the standby's window reads "
                f"{simulate.receipt_outcome(carried[-1])} for the "
                "pending admission instead of the pending view — the "
                "staging never formed; the pinned release predates "
                "the adopted-receipt-regression contract"
            )
        digest_entries.append(
            {"phase": "stale", "standby_view": "accepted"}
        )

        # Phase 4 — the owner's own settle: its field-owning boundary
        # applies the admission and journals the terminal verdict.
        owner_scan = pair.scan(duty_url, failures)
        settled = index_receipt(duty_url, index, "GET /checkpoint",
                                failures)
        evidence["settled"] = verdict(settled) if settled else None
        if settled is None or not admission_hit(settled, command, ACTOR):
            failures.append(
                f"the owner's served log lost the admission at index "
                f"{index} — {settled}"
            )
            raise Abort
        if not str(verdict(settled)).startswith("applied@"):
            failures.append(
                "receipt-regression-failed: the field owner never "
                f"applied the admission — its served log reads "
                f"{verdict(settled)}"
            )
            raise Abort
        if simulate.snapshot_point(owner_scan, point) != {"bool": value}:
            failures.append(
                f"the owner's image reads "
                f"{simulate.snapshot_point(owner_scan, point)} for "
                f"point {point}, expected the command's value "
                f"{{'bool': {value}}}"
            )
            raise Abort
        digest_entries.append({"phase": "settled", "view": verdict(settled)})

        # Phase 5 — the adoption of the staler view: the owner
        # demotes and its tracking pull lands on the standby's window,
        # whose view of the admission's own index is the staler
        # `accepted`. The merge must refuse the regression: the
        # owner's receipt keeps its terminal verdict, the admission is
        # not re-queued, and the field sees no second application.
        rig.demote(duty_url, failures, what="the settled field owner")
        observations = {duty_name: [verdict(settled)],
                        standby_name: []}
        roles = []
        scan = owner_scan
        for _ in range(WATCH_SCANS):
            scan = pair.scan(duty_url, failures)
            report = pair.get(f"{duty_url}/role", "GET /role", failures)
            roles.append(report.get("role"))
            observed = index_receipt(
                duty_url, index, "GET /checkpoint", failures
            )
            observations[duty_name].append(
                verdict(observed) if observed is not None else None
            )
            if claim_reclaim.tracking(report):
                break
        evidence["adoption_roles"] = roles
        evidence["owner_views"] = observations[duty_name]
        after = index_receipt(duty_url, index, "GET /checkpoint",
                              failures)
        if after is None or not admission_hit(after, command, ACTOR):
            failures.append(
                f"the owner's served log lost the admission across the "
                f"stale-view adoption — {after}"
            )
            raise Abort
        if simulate.snapshot_point(scan, point) != {"bool": value}:
            failures.append(
                "receipt-regression-failed: the stale-view adoption "
                "re-applied the command on the field — point "
                f"{point} reads "
                f"{simulate.snapshot_point(scan, point)} where the "
                f"settled value {{'bool': {value}}} stood"
            )
            raise Abort
        digest_entries.append(
            {"phase": "adopt", "roles": roles[:1],
             "view": verdict(after)}
        )

        # Phase 6 — the covering adoption: the standby is scanned so
        # its own window catches up, and the pair's pulls confirm the
        # terminal verdict on both sides.
        for _ in range(WINDOW_SCANS):
            pair.scan(standby_url, failures)
            time.sleep(FETCH_SETTLE_S)
            standby_rows = admission_receipts(
                served(standby_url, "GET /receipts", failures),
                command, ACTOR,
            )
            observations[standby_name].append(
                verdict(standby_rows[-1]) if standby_rows else None
            )
            duty_rows = admission_receipts(
                served(duty_url, "GET /receipts", failures),
                command, ACTOR,
            )
            observations[duty_name].append(
                verdict(duty_rows[-1]) if duty_rows else None
            )
        evidence["observations"] = observations

        fault = regression_fault(observations, tamper)
        if fault is not None:
            if tamper is None and pre_contract_shape(observations):
                raise Inconclusive(
                    "the pinned release predates the "
                    "adopted-receipt-regression contract — the "
                    "adoption window read the documented "
                    f"last-pull-wins shape: {fault[1]}"
                )
            failures.append(fault[0] + ": " + fault[1])
            raise Abort
        digest_entries.append(
            {
                "phase": "window",
                "observed": {
                    name: [value for value in reads]
                    for name, reads in observations.items()
                },
            }
        )

        # Phase 7 — the journals: exactly one `command_settled` per
        # admission on each peer's served journal and each declared
        # durable file.
        journaled = {}
        for name, url in peers.items():
            entries = pair.get(f"{url}/journal", "GET /journal", failures)
            journaled[name] = settle_rows(entries, command, ACTOR)
        durable = {}
        for name, files in (
            (duty_name, rig.duty_files),
            (standby_name, rig.standby_files),
        ):
            path = files["journal_file"]
            if not os.path.exists(path):
                raise Inconclusive(
                    f"{name}'s declared journal file {path} does not "
                    "exist — the pinned release did not honor the "
                    "--journal-file persistence the durable audit needs"
                )
            durable[name] = settle_rows(
                journal_entries(path), command, ACTOR
            )
        for name in peers:
            rows = journaled[name]
            if len(rows) > 1:
                failures.append(
                    "receipt-regression-nondeterministic: "
                    f"{name}'s served journal carries {len(rows)} "
                    "command_settled records for one admission "
                    f"{rows} — the re-settle the finding recorded"
                )
            elif not rows:
                failures.append(
                    "receipt-regression-failed: "
                    f"{name}'s served journal never settled the "
                    "admission the adoption window audited"
                )
            if len(durable[name]) > 1:
                failures.append(
                    "receipt-regression-nondeterministic: "
                    f"{name}'s durable journal carries "
                    f"{len(durable[name])} command_settled records for "
                    f"one admission {durable[name]}"
                )
            elif not durable[name]:
                failures.append(
                    "receipt-regression-failed: "
                    f"{name}'s durable journal never settled the "
                    "admission the adoption window audited"
                )
        if failures:
            raise Abort
        evidence["journaled"] = journaled
        evidence["durable"] = durable
        digest_entries.append(
            {"phase": "journaled", "journaled": journaled}
        )

        # Phase 8 — the launch roles: the manifest's field owner is
        # promoted back and the pair reconverges `active` plus
        # `tracking`.
        promoted = rig.promote(
            duty_url,
            failures,
            what="the manifest's field owner",
            note=" restoring the launch roles",
        )
        duty_role = standby_role = None
        for _ in range(RESTORE_TICKS):
            pair.scan(standby_url, failures)
            pair.scan(duty_url, failures)
            duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
            standby_role = pair.get(
                f"{standby_url}/role", "GET /role", failures
            )
            if duty_role.get("role") == "active" and claim_reclaim.tracking(
                standby_role
            ):
                break
        if duty_role.get("role") != "active":
            failures.append(
                f"the restored field owner reports "
                f"{duty_role.get('role')!r}, expected active — the "
                "pair was not left in its launch roles"
            )
        if not claim_reclaim.tracking(standby_role):
            failures.append(
                "the restored standby never reconverged tracking — "
                f"GET /role answers {standby_role}"
            )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "restore",
                "promote": promoted.get("role"),
                "duty_role": duty_role.get("role"),
                "standby_role": claim_reclaim.sync_state(standby_role),
            }
        )
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Inconclusive:
        raise
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        if rig is not None:
            rig.close()
    return digest_entries, evidence, failures


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plant-server", required=True)
    parser.add_argument("--controller", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--dynamics", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument(
        "--tamper",
        choices=["expect-regressed"],
        help="doctor the run — accept a served `accepted` for an "
        "admission whose settle already journaled, the defect the "
        "contract closed; the case must fail naming the diagnostic",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = regression_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                f"receipt-regression: the {args.tamper} case wanted "
                "the run to surface its named diagnostic — an "
                "inconclusive run offers the doctored case no evidence"
            )
            return 1
        eprint(f"receipt-regression: inconclusive — {inconclusive}")
        print(
            f"receipt-regression-digest inconclusive — {inconclusive}"
        )
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"receipt-regression: {line}")
        return 1
    for failure in failures:
        eprint(f"receipt-regression: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"receipt-regression: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored "
                "expectation"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"receipt-regression-digest {digest} — one admission applied "
        "on the field owner while its standby held a staler pending "
        "view of the same index, the owner's adoption of that staler "
        "window kept the terminal verdict, one command_settled per "
        "admission on both served and durable journals, the launch "
        "roles restored"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())