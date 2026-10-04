#!/usr/bin/env python3
"""The settled-receipt-arbitration leg for the reference plant — the
consumer-side proof that a pair holding one admission settled at one
submission index to two *different* terminal verdicts converges on one
arbitrated verdict instead of handing the index back and forth
(WW-ENG-003, WW-LCM-001 — the settled-receipt contract #690's fix
establishes, mirrored at the customer boundary from the rig's
settled-receipt-arbitration leg).

The contract: adoption of a settled receipt is idempotent or
arbitrated, never last-pull-wins between contradictory terminal
verdicts, and a settlement journals once per admission. The defect had
two peers holding different settled outcomes at one submission index
ping-ponging the outcome on every mutual adoption (~5-8 Hz), flooding
both durable journals with phantom `command_settled` lines, wrapping
the served ring, and flapping the consumer-facing `/receipts` until a
promote ended it.

The declared pair is already driven — every pull a peer performs
happens inside its own `POST /scan` — which is what makes the
reproduction the fix names deterministic here, with no freeze and no
timing race:

- converges the declared pair to its launch roles — the field owner
  `active`, the standby `tracking` — through the pair rig's driven-tick
  loop, and gates the contract's surface: each peer's served
  checkpoint must carry the receipt window and admission counters the
  audit correlates by, each receipt must carry the declared
  `actor`/`reason`, and the manifest must declare the journal files
  the durable half reads. A pinned release predating that surface
  reports `settled-arbitration-digest inconclusive`, never a failure;
- lets the tracking member's run clock run ahead of the owner's — its
  pulls hold the run clock while its own scans tick it — so the
  promotion boundary's final-sync fetch meets a document stale by that
  clock and *carries* the still-pending admission rather than adopting
  it;
- submits a receipted `write_value` on the declared writable point
  without scanning the owner, so the admission stays pending across
  the boundary pull, then promotes the tracking member: the promoted
  peer settles the carried admission at its own boundary and the
  fenced owner's next scan settles the same admission at its own. Two
  lines, one submission index, two different apply ticks — the
  contradictory settled pair, each peer recording only its own
  settlement;
- demotes the promoted peer, leaving both lines following each other —
  the dual-standby adoption window the ping-pong ran in — and audits
  every observation through both peers' serving monitors: the two
  served receipt logs agree on one arbitrated verdict for the
  admission, and that verdict never moves under the poll;
- reads both declared `--journal-file`s: at most one `command_settled`
  per admission on each peer, while the two peers' journals still
  record their own different first settlements — the evidence the
  contradiction was staged and arbitrated rather than never produced;
- promotes the reconciled holder back and audits through the switch:
  the peers reconverge `active` plus `tracking` on one identical
  adopted receipt log, and the pair ends on its launch roles.

Usage:

    settled_receipt_arbitration.py --plant-server PATH \
        --controller PATH --model model/plant.json \
        --dynamics model/dynamics.json --scenario ci/scenario.json \
        --manifest deploy/manifest.json

On success one `settled-arbitration-digest <sha256>` line prints — the
check runs two passes and compares them
(`settled-arbitration-nondeterministic`). A contract violation reports
`settled-arbitration: …` lines on stderr and exits 1 — the check's
`settled-arbitration-failed`. `--tamper contradictory-pair` audits the
pre-arbitration observation — the pair asserted converged while each
peer still serves its own contradictory verdict — and must surface the
named diagnostic (`settled-arbitration-unchecked`).
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
# doctored case: the contradictory settled pair asserted converged
# while both peers still serve their own verdicts — the leg's own
# audit can no longer be trusted to catch what it names.
LEG = {
    "order": 595,
    "title": "the settled-receipt-arbitration leg",
    "passes": "settled-receipt-arbitration",
    "failed": "settled-arbitration-failed",
    "tampers": [
        {
            "name": "contradictory-pair",
            "passed": "a contradictory-pair case passed the settled-receipt-arbitration leg",
            "missed": "the contradictory-pair case did not report its named diagnostic",
            "evidence": ["a contradictory pair"],
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


# The settle beat each requested checkpoint fetch gets to land inside
# before the next driven scan consumes it, and the driven-scan bounds
# each phase runs: the standby's clock lead over the owner's, the
# promoted peer's boundary settle, the owner's own settle, the
# dual-standby adoption window, and the reconciled switch.
FETCH_SETTLE_S = 0.3
LEAD_SCANS = 2
SETTLE_SCANS = 2
WINDOW_SCANS = 6
RECONVERGE_SCANS = 6

# The admission's declared identity — unique in both peers' receipt
# logs, the (command, actor) pair the audit correlates by.
ACTOR = "ci-settled-arbitration"
REASON = "settled-receipt-arbitration"


def applied_tick(receipt):
    """The apply tick a settled `applied` receipt carries, or None."""
    applied = receipt.get("outcome", {}).get("applied") or {}
    tick = applied.get("tick")
    return tick if isinstance(tick, int) else None


def verdict(receipt):
    """A receipt's normalized verdict — the outcome name beside the
    apply tick. The tick rides it because the contradiction the fix
    arbitrates is two `applied` verdicts at two apply ticks: a serving
    log that answers only `applied` cannot tell convergence from
    flapping."""
    outcome = simulate.receipt_outcome(receipt)
    tick = applied_tick(receipt)
    return f"{outcome}@{tick}" if tick is not None else outcome


def admission_receipts(receipts, command):
    """The receipts answering `command` in a served receipt log — the
    admission's own records, newest last."""
    return [receipt for receipt in receipts if receipt.get("command") == command]


def settle_rows(entries):
    """`(seq, tick, actor, outcome)` per `command_settled` record —
    the normalized settle row the digest compares and the audit
    bounds."""
    rows = []
    for entry in entries:
        event = entry.get("event") or {}
        receipt = (event.get("command_settled") or {}).get("receipt")
        if not receipt:
            continue
        rows.append(
            {
                "seq": entry.get("seq"),
                "tick": entry.get("tick"),
                "actor": receipt.get("actor"),
                "outcome": verdict(receipt),
            }
        )
    return rows


def journal_entries(path):
    """A `--journal-file`'s journal entries in file order — the durable
    half of the audit. The shared reader also returns the run-boundary
    markers; those are lifetimes, not entries."""
    return [entry for kind, entry in pair.journal_records(path)
            if kind == "entry"]


def admission_settles(entries, command):
    """The `command_settled` records in `entries` whose receipt answers
    `command`."""
    found = []
    for entry in entries:
        event = entry.get("event") or {}
        receipt = (event.get("command_settled") or {}).get("receipt")
        if receipt and receipt.get("command") == command:
            found.append(entry)
    return found


def receipt_window(url, what, failures):
    """The served receipt log and admission counters one peer's
    checkpoint carries — the surface the audit correlates absolute
    submission indices by. Returns the document."""
    checkpoint = pair.get(f"{url}/checkpoint", what, failures)
    if not isinstance(checkpoint.get("receipts"), list):
        raise Inconclusive(
            f"{what} carries no receipt window — the pinned release "
            "predates the settled-receipt-arbitration contract"
        )
    admission = checkpoint.get("command_admission")
    if not isinstance(admission, dict) \
            or not isinstance(admission.get("attempts"), int):
        raise Inconclusive(
            f"{what} carries no admission counters — the pinned "
            "release predates the settled-receipt-arbitration contract"
        )
    return checkpoint


def served(url, what, failures):
    """One peer's served receipt log."""
    return pair.get(f"{url}/receipts", what, failures)


def following(url, failures):
    """The peer's role report while it is a standby following a source
    — `tracking` behind a field owner, or the ownerless `orphaned`
    verdict a line owes while the source it follows owns no field.
    None while the peer owns the field or has adopted nothing."""
    report = pair.get(f"{url}/role", "GET /role", failures)
    if report.get("role") != "standby":
        return None
    sync = report.get("sync")
    if isinstance(sync, dict) and (
        "tracking" in sync or "orphaned" in sync
    ):
        return report
    return None


def window_fault(observed):
    """The named fault the adoption window's observations carry, or
    None when the pair converged on one arbitrated verdict that never
    moved: `settled-arbitration-failed` while a peer serves no
    terminal verdict for the admission at all, and
    `settled-arbitration-nondeterministic` while the served receipts
    move under the poll or the two peers' logs disagree — the
    oscillation the finding recorded, or the contradiction left
    unarbitrated."""
    if not observed:
        return (
            "settled-arbitration-failed",
            "the adoption window carried no observation — neither "
            "peer's served receipts ever read",
        )
    if len({tuple(sorted(poll.items())) for poll in observed}) > 1:
        return (
            "settled-arbitration-nondeterministic",
            "the served receipts moved across the adoption window — "
            f"the audited verdict is not stable, polls {observed}",
        )
    for poll in observed:
        if any(value is None for value in poll.values()):
            return (
                "settled-arbitration-failed",
                "a peer served no terminal verdict for the admission "
                f"across the adoption window — polls {observed}",
            )
        if len(set(poll.values())) > 1:
            return (
                "settled-arbitration-nondeterministic",
                "the peers' served receipts still disagree across the "
                f"adoption window — a contradictory pair reads {poll} "
                "with no convergence and no arbitration",
            )
    return None


def arbitration_pass(args, tamper):
    """The settled-receipt-arbitration run: converge, stage the
    contradictory settled pair across the promotion boundary's carry,
    hold both lines in the dual-standby adoption window, audit the
    convergence and the bounded journals, and restore the launch
    roles. Returns `(digest_entries, evidence, failures)`; raises
    `Inconclusive` where the pinned release predates the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "settled-receipt-arbitration leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    duty_name, standby_name = duty_decl["name"], standby_decl["name"]
    with open(args.model) as handle:
        model = json.load(handle)
    point = force_carryover.force_target(model)
    if point is None:
        raise Abort(
            "the emitted model declares no writable In point at "
            f"{force_carryover.FORCE_SIGNAL} — the "
            "settled-receipt-arbitration leg has nothing to race"
        )

    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        peers = {duty_name: duty_url, standby_name: standby_url}

        # Phase 1 — convergence: the pair rig's driven-tick loop, the
        # tracking peer scanned first so each pull applies the owner's
        # latest checkpoint and the peers rest identical.
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
        # receipt window and admission counters the audit correlates
        # by, and the manifest must declare the journal files the
        # durable half reads. A surface absent is the release
        # predating the contract, never a violation of it.
        for name, url in peers.items():
            receipt_window(url, f"GET /checkpoint on {name}", failures)
        if (
            rig.duty_files.get("journal_file") is None
            or rig.standby_files.get("journal_file") is None
        ):
            raise Inconclusive(
                "the manifest's pair declares no journal_file "
                "persistence — the durable half of the arbitration "
                "audit is absent"
            )

        baseline = simulate.snapshot_point(converged["owner"], point)
        if baseline is None or "bool" not in baseline:
            failures.append(
                f"the raced admission's target point {point} serves "
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

        # Phase 2 — the standby's clock lead: its tracking pulls hold
        # the run clock while its own scans tick it, so the promotion
        # boundary below meets a document stale by that clock and
        # carries the still-pending admission rather than adopting it.
        for _ in range(LEAD_SCANS):
            pair.scan(standby_url, failures)
            time.sleep(FETCH_SETTLE_S)

        # Phase 3 — the raced admission and the boundary carry: the
        # receipted write lands pending on the field owner — never
        # scanned, so its own apply boundary stays closed across the
        # promotion's pull.
        status, receipt = pair.request(
            f"{duty_url}/command",
            {"command": command, "actor": ACTOR, "reason": REASON},
        )
        if status != 200 or simulate.receipt_outcome(receipt) != "accepted":
            failures.append(
                f"the raced admission answered {status} {receipt}, "
                "expected an accepted receipt"
            )
            raise Abort
        if receipt.get("actor") != ACTOR or receipt.get("reason") != REASON:
            raise Inconclusive(
                "the served receipt drops the declared actor/reason — "
                "the submission-record identity the audit correlates "
                "by; the pinned release predates the "
                "settled-receipt-arbitration contract"
            )
        digest_entries.append(
            {
                "phase": "admit",
                "receipt": receipt,
            }
        )

        # Phase 4 — the promoted peer settles the carried admission at
        # its own boundary, then the fenced owner's next scan settles
        # the same admission at its own. Two lines, one submission
        # index, two different apply ticks.
        rig.promote(
            standby_url,
            failures,
            what="the tracking peer",
            note=" racing the owner's pending admission",
        )
        for _ in range(SETTLE_SCANS):
            pair.scan(standby_url, failures)
            time.sleep(FETCH_SETTLE_S)
        pair.scan(duty_url, failures)
        time.sleep(FETCH_SETTLE_S)

        staged = {}
        for name, url in peers.items():
            rows = admission_receipts(
                served(f"{url}/receipts", "GET /receipts", failures),
                command,
            )
            if not staged:
                staged = {name: rows}
            staged[name] = rows
        evidence["staged"] = {
            name: [verdict(receipt) for receipt in rows]
            for name, rows in staged.items()
        }
        firsts = {
            name: (verdict(rows[-1]) if rows else None)
            for name, rows in staged.items()
        }
        digest_entries.append(
            {
                "phase": "staged",
                "settled": evidence["staged"],
            }
        )
        # The contradiction the fix arbitrates: both peers settled the
        # admission, each at its own tick. A pair that settled one
        # admission identically never presented the defect, so the
        # staging is reported rather than asserted away.
        if None in firsts.values() or len(set(firsts.values())) < 2:
            failures.append(
                "settled-arbitration-failed: the promotion boundary "
                "never staged the contradictory settled pair — the "
                f"peers' own settlements read {firsts}; the leg has "
                "no arbitration to audit"
            )
            raise Abort

        # Phase 5 — the dual-standby adoption window: the promoted
        # peer's documented demote leaves both lines following each
        # other, the mutual adoption the ping-pong ran in.
        rig.demote(standby_url, failures, what="the promoted peer")
        incumbent = None
        postures = []
        for _ in range(RECONVERGE_SCANS):
            pair.scan(duty_url, failures)
            time.sleep(FETCH_SETTLE_S)
            report = pair.get(f"{duty_url}/role", "GET /role", failures)
            postures.append(claim_reclaim.sync_state(report))
            if report.get("role") == "standby" and postures[-1] in (
                "tracking",
                "orphaned",
            ):
                incumbent = report
                break
        evidence["demote_watch"] = postures
        if incumbent is None:
            failures.append(
                "the demoted promoted peer never became a standby "
                f"following the field owner — postures {postures}"
            )
            raise Abort
        window = []
        for _ in range(WINDOW_SCANS):
            pair.scan(standby_url, failures)
            time.sleep(FETCH_SETTLE_S)
            pair.scan(duty_url, failures)
            time.sleep(FETCH_SETTLE_S)
            served_now = {
                name: admission_receipts(
                    served(f"{url}/receipts", "GET /receipts", failures),
                    command,
                )
                for name, url in peers.items()
            }
            window.append(
                {
                    name: (verdict(rows[-1]) if rows else None)
                    for name, rows in served_now.items()
                }
            )
        evidence["window"] = window
        digest_entries.append(
            {"phase": "window", "window": window}
        )
        # Under the doctored case the audit reads the
        # pre-arbitration observation — the pair asserted converged
        # while each peer still serves its own contradictory verdict.
        observed = window if tamper != "contradictory-pair" else [
            {name: firsts[name] for name in peers}
        ]
        fault = window_fault(observed)
        if fault is not None:
            failures.append(fault[0] + ": " + fault[1])
            raise Abort

        # Phase 6 — the served journals: at most one settle
        # transition per admission on each peer, while the two peers
        # still record their own different first settlements — the
        # evidence the contradiction was staged and arbitrated rather
        # than the pair converging because it never diverged.
        journaled = {}
        for name, url in peers.items():
            entries = pair.get(f"{url}/journal", "GET /journal", failures)
            journaled[name] = admission_settles(entries, command)
        for name, entries in journaled.items():
            rows = settle_rows(entries)
            if len(rows) > 1:
                failures.append(
                    "settled-arbitration-nondeterministic: "
                    f"{name}'s served journal carries {len(rows)} "
                    "command_settled records for one admission "
                    f"{rows} — the settlement must journal once per "
                    "admission"
                )
            if not rows:
                failures.append(
                    "settled-arbitration-failed: "
                    f"{name}'s served journal never settled the "
                    "admission the arbitration window audited"
                )
        if failures:
            raise Abort
        evidence["journaled"] = {
            name: settle_rows(entries)
            for name, entries in journaled.items()
        }
        digest_entries.append(
            {
                "phase": "journaled",
                "journaled": evidence["journaled"],
            }
        )

        # Phase 7 — the durable half: each declared journal file
        # carries the same bounded settle record its served monitor
        # did.
        durable = {}
        for name, files in (
            (duty_name, rig.duty_files),
            (standby_name, rig.standby_files),
        ):
            path = files["journal_file"]
            if not os.path.exists(path):
                failures.append(
                    f"{name}'s declared journal file {path} does not "
                    "exist — the --journal-file flag was not honored"
                )
                continue
            durable[name] = settle_rows(
                admission_settles(journal_entries(path), command)
            )
        for name in peers:
            if name not in durable:
                continue
            if len(durable[name]) > 1:
                failures.append(
                    "settled-arbitration-nondeterministic: "
                    f"{name}'s durable journal carries "
                    f"{len(durable[name])} command_settled records "
                    f"for one admission {durable[name]} — the "
                    "settlement must journal once per admission"
                )
            if durable[name] and journaled.get(name) \
                    and [row["outcome"] for row in durable[name]] != [
                        row["outcome"] for row in journaled[name]
                    ]:
                failures.append(
                    "settled-arbitration-failed: "
                    f"{name}'s durable journal carries "
                    f"{durable[name]} where its served journal "
                    f"carries {settle_rows(journaled[name])} — the "
                    "durable audit is not the served audit"
                )
        if failures:
            raise Abort
        evidence["durable"] = durable
        digest_entries.append({"phase": "durable", "durable": durable})

        # Phase 8 — the launch roles: the reconciled holder takes the
        # field back through the documented switch and the pair
        # reconverges on one identical adopted receipt log.
        restored = rig.switch(
            standby_url,
            duty_url,
            failures,
            demote_what="the reconciled holder",
            promote_what="the manifest's field owner",
            promote_note=" restoring the launch roles",
            audit_receipts=True,
        )
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        standby_role = pair.get(
            f"{standby_url}/role", "GET /role", failures
        )
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
        evidence["restored_receipts"] = [
            verdict(receipt)
            for receipt in restored["receipts"]
        ]
        digest_entries.append(
            {
                "phase": "restore",
                "demote": restored["demote"].get("role"),
                "promote": restored["promote"].get("role"),
                "duty_role": duty_role.get("role"),
                "standby_role": claim_reclaim.sync_state(standby_role),
                "receipts": evidence["restored_receipts"],
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
        choices=["contradictory-pair"],
        help="doctor the run — audit the pre-arbitration "
        "observation, the pair asserted converged while each peer "
        "still serves its own contradictory verdict; the case must "
        "fail naming the diagnostic",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = arbitration_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                f"settled-arbitration: the {args.tamper} case wanted "
                "the run to surface its named diagnostic — an "
                "inconclusive run offers the doctored case no evidence"
            )
            return 1
        eprint(f"settled-arbitration: inconclusive — {inconclusive}")
        print(
            f"settled-arbitration-digest inconclusive — "
            f"{inconclusive}"
        )
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"settled-arbitration: {line}")
        return 1
    for failure in failures:
        eprint(f"settled-arbitration: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"settled-arbitration: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored "
                "evidence"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"settled-arbitration-digest {digest} — one admission "
        "settled to two verdicts at one index across the promotion "
        "boundary's carry, converged to the arbitrated verdict "
        "inside the dual-standby window, one command_settled per "
        "admission per peer journal, the launch roles restored"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
