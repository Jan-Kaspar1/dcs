#!/usr/bin/env python3
"""The receipt-index-collision leg for the reference plant — the
consumer-side proof that a settled submission is never silently
replaced by a different command's receipt at the same absolute
submission index, so the pair's audit keeps every admitted command's
terminal verdict (WW-ENG-003, WW-LCM-001 — the contract #775's fix
establishes, mirrored at the customer boundary from the rig's
receipt-index-collision leg).

The contract: admission indices derive from a per-peer attempts
counter that converges only through checkpoint adoption, so inside the
promote/fence window both lines can mint receipts at the same absolute
index for different commands and whichever line wins convergence
displaces the other's record. The served `/receipts` must keep a
servable record of every admitted command rather than replacing a
settled submission at a reused index — reuse, not aging, which the
eviction's `receipt_base` gap does not cover. What the fix names as
the displaced admission's preserved audit: the merge adjudicates the
collision through the merge — the displaced receipt re-mints past the
adopted window's high-water with its command, actor, and terminal
`superseded` verdict intact — and the recorder's re-home reports the
already-journaled settle's new index rather than settling it a second
time.

The declared pair is driven — every pull a peer performs happens
inside its own `POST /scan` — so the promote/fence window is staged by
which peer is scanned when, with no freeze and no timing race. The
run:

- converges the declared pair to its launch roles — the manifest's
  field owner `active`, the standby `tracking` — through the pair
  rig's driven-tick loop, and gates the contract surface: each peer's
  served checkpoint must carry the receipt window and admission
  counters the audit correlates by, each receipt must carry the
  declared `actor`/`reason`, and the manifest must declare the journal
  files the durable half reads. A pinned release predating that
  surface reports `receipt-collision-digest inconclusive`, never a
  failure;
- records the owner's submission high-water, then promotes the
  standby *first*: its promotion-boundary final-sync fetch meets only
  the pre-admission document, so its served window can never cover the
  admission the run lands next, and its unconditional claim preempts
  the incumbent;
- submits a receipted `write_value` on the declared writable point on
  the still-field-owning incumbent inside that window — the admission
  mints at the index the sibling is about to mint from — then drives
  the incumbent's detection scans until it demotes, the fence
  superseding its pending admission;
- submits a *second* receipted `write_value` on the new active,
  minting the same absolute index: the split mint the collision is;
- scans the new active so its own admission applies, then the
  demoted incumbent, whose adoption of that window re-mints the
  displaced receipt past the window's high-water;
- audits both peers' serving monitors and both declared durable
  journals: the incumbent's `/receipts` carries both admissions as
  distinct records at distinct indices — the successor's applied
  verdict at the contested index and the displaced one's superseded
  verdict past it — and each peer carries exactly one
  `command_settled` per admission;
- promotes the manifest's field owner back and audits the pair
  reconverging `active` plus `tracking` — the pair ends on its launch
  roles.

The contract postdates the release line's v0.3.0 cut: where the
launched tooling predates it the run's own evidence is the
pre-contract shape — a checkpoint carrying no receipt window or
admission counters, a receipt dropping its declared `actor`/`reason`,
the promoted sibling's window already covering the raced admission,
or a fence that never superseded it — and the leg reports
`receipt-collision-digest inconclusive` rather than asserting until
the manifest repins a release carrying the contract.

Usage:

    receipt_index_collision.py --plant-server PATH \
        --controller PATH --model model/plant.json \
        --dynamics model/dynamics.json --scenario ci/scenario.json \
        --manifest deploy/manifest.json

On success one `receipt-collision-digest <sha256>` line prints — the
check runs two passes and compares them
(`receipt-collision-nondeterministic`). A contract violation reports
`receipt-collision: …` lines on stderr and exits 1 — the check's
`receipt-collision-failed`. `--tamper expect-displaced` doctors the
leg's own expectation to the defect shape — accepting the displaced
admission as served while only the successor's receipt stands at the
contested index — so the leg proves its collision audit fires on the
honest re-minted record rather than passing an unexercised contract.
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
# doctored case: the leg accepting the displaced admission as served
# while only the successor's receipt stands at the contested index —
# the silent displacement the contract closed — must surface the named
# diagnostic on the honest re-minted record.
LEG = {
    "order": 615,
    "title": "the receipt-index-collision leg",
    "passes": "receipt-index-collision",
    "tampers": [
        {
            "name": "expect-displaced",
            "passed": "an expect-displaced case passed the receipt-index-collision leg",
            "missed": "the expect-displaced case did not report its named diagnostic",
            "evidence": ["the doctored expectation accepted the displaced admission"],
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


# The driven-scan bounds each phase runs: the promoted sibling's
# settle into the field-owning role, the fenced demote's detection
# scans, the demoted peer's reconvergence, and the launch-role hold
# train.
FETCH_SETTLE_S = 0.3
SETTLE_SCANS = 6
WATCH_SCANS = 8
RESOLVE_SCANS = 8
RESTORE_TICKS = 4

# The two admissions' declared identities — each unique in both peers'
# receipt logs, the (command, actor) pairs the audit correlates by.
RACED_ACTOR = "ci-receipt-collision-raced"
MINTED_ACTOR = "ci-receipt-collision-minted"
REASON = "receipt-index-collision"


def verdict(receipt):
    """A receipt's normalized verdict — the outcome name beside the
    apply tick, the identity the collision audit compares. The tick
    rides it because the pair's two admissions share one index and
    must stay tellable apart by what each line recorded."""
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
    half of the audit."""
    return [
        record
        for kind, record in pair.journal_records(path)
        if kind == "entry"
    ]


def settle_rows(entries, command, actor):
    """`(seq, tick, verdict)` per `command_settled` record answering
    the admission — the normalized settle row the digest compares and
    the audit bounds. The verdict carries the apply tick so a journal
    record and a served receipt are read in one vocabulary: the audit
    compares a journaled settle against a served receipt, and the
    two colliding admissions share one index."""
    rows = []
    for entry in entries:
        event = entry.get("event") or {}
        receipt = (event.get("command_settled") or {}).get("receipt")
        if receipt is None or not admission_hit(receipt, command, actor):
            continue
        rows.append(
            (entry.get("seq"), entry.get("tick"), verdict(receipt))
        )
    return rows


def receipt_window(url, what, failures):
    """The served checkpoint's retained receipt tail and the absolute
    submission index of its first entry — the (log, high-water) pair
    the collision audit correlates absolute submission indices by. The
    surface gate: a window or its admission counters absent is the
    release predating the contract, never a violation of it."""
    checkpoint = pair.get(f"{url}/checkpoint", what, failures)
    receipts = checkpoint.get("receipts")
    attempts = (checkpoint.get("command_admission") or {}).get("attempts")
    if not isinstance(receipts, list):
        raise Inconclusive(
            f"{what} carries no receipt window — the pinned release "
            "predates the receipt-index-collision contract"
        )
    if not isinstance(attempts, int):
        raise Inconclusive(
            f"{what} carries no admission counters — the pinned "
            "release predates the receipt-index-collision contract"
        )
    return receipts, max(0, attempts - len(receipts))


def next_receipt_index(url, what, failures):
    """The absolute submission index the peer's next `POST /command`
    mints — the window's high-water `base + len(receipts)`."""
    receipts, base = receipt_window(url, what, failures)
    return base + len(receipts)


def collision_fault(observed, tamper):
    """The named fault the post-convergence audit carries, or None when
    every admitted command kept its retrievable terminal verdict and
    one settle per admission: `receipt-collision-failed` while the
    displaced admission's terminal verdict is nowhere the pair serves
    or journals — only the successor's record standing at the
    contested index — or while an admission never reached a terminal
    verdict at all, and `receipt-collision-nondeterministic` while a
    retrievable verdict is not one verdict across the pair, the two
    admissions answered with one shared record, or a peer recorded an
    admission twice. Under the leg's own doctored case the displaced
    admission is accepted as served, so the case surfaces the named
    diagnostic on the honest re-minted record."""
    if tamper == "expect-displaced":
        return (
            "receipt-collision-failed",
            "the doctored expectation accepted the displaced "
            f"admission — the audit read {observed}",
        )
    for key, admission in observed.items():
        served_verdicts = [
            value for value in admission["served"].values()
            if value is not None
        ]
        terminal = [
            value for value in served_verdicts
            if not value.startswith("accepted")
        ]
        journaled = [
            value for name in admission["journaled"].values()
            for value in (row[2] for row in name)
        ]
        durable = [
            row[2]
            for rows in admission["durable"].values()
            for row in rows
        ]
        if not terminal and not journaled and not durable:
            return (
                "receipt-collision-failed",
                f"the {key} admission has no retrievable terminal "
                "verdict on the pair's served surface or journals "
                "where only the colliding successor's receipt stands "
                "at the index — a settled submission silently "
                "replaced",
            )
        for name, rows in admission["journaled"].items():
            if len(rows) > 1:
                return (
                    "receipt-collision-nondeterministic",
                    f"{name}'s served journal records the {key} "
                    f"admission {len(rows)} times {rows}",
                )
            if len(admission["durable"].get(name, [])) > 1:
                return (
                    "receipt-collision-nondeterministic",
                    f"{name}'s durable journal records the {key} "
                    f"admission "
                    f"{len(admission['durable'][name])} times",
                )
        verdicts = set(terminal) | set(journaled) | set(durable)
        if len(verdicts) > 1:
            return (
                "receipt-collision-nondeterministic",
                f"the {key} admission's retrievable verdict is not one "
                f"verdict across the pair's surfaces — "
                f"{sorted(verdicts)}",
            )
    for name in observed["raced"]["served"]:
        raced = observed["raced"]["served"][name]
        minted = observed["minted"]["served"][name]
        if raced is not None and minted is None:
            return (
                "receipt-collision-nondeterministic",
                f"{name} serves the displaced admission's record "
                "where the colliding successor's own admission has "
                "none — the two submissions collapsed onto one record",
            )
    return None


def collision_pass(args, tamper):
    """The receipt-index-collision run: converge, promote the standby
    so its window never covers the coming admission, race a receipted
    admission on the preempted holder, mint the same absolute index on
    the new active, converge, audit both monitors and both durable
    journals for every admitted command's retrievable verdict and one
    settle per admission, and restore the launch roles. Returns
    `(digest_entries, evidence, failures)`; raises `Inconclusive`
    where the pinned release predates the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "receipt-index-collision leg has nothing to exercise"
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
            "receipt-index-collision leg has nothing to admit"
        )

    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        peers = {duty_name: duty_url, standby_name: standby_url}

        # Phase 1 — convergence and the contract surface gate.
        converged = rig.converge(failures)
        evidence["converged"] = converged["ticks"][-1]
        digest_entries.append(
            {
                "phase": "converge",
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )
        for name, url in peers.items():
            receipt_window(url, f"GET /checkpoint on {name}", failures)
        if (
            rig.duty_files.get("journal_file") is None
            or rig.standby_files.get("journal_file") is None
        ):
            raise Inconclusive(
                "the manifest's pair declares no journal_file "
                "persistence — the durable half of the collision "
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
        raced_command = takeover.write_value(point, not baseline["bool"])
        minted_command = takeover.write_value(point, baseline["bool"])
        digest_entries.append(
            {
                "phase": "gate",
                "point": point,
                "baseline": baseline,
                "raced": raced_command,
                "minted": minted_command,
            }
        )

        # Phase 2 — the preempting promote lands before the admission:
        # the sibling's boundary fetch meets the pre-admission
        # document, so its served window can never cover the command
        # the run races onto the preempted holder.
        index = next_receipt_index(duty_url, "GET /checkpoint", failures)
        rig.promote(standby_url, failures, what="the tracking sibling")
        postures = []
        promoted = None
        report = None
        for _ in range(SETTLE_SCANS):
            pair.scan(standby_url, failures)
            time.sleep(FETCH_SETTLE_S)
            report = pair.get(
                f"{standby_url}/role", "GET /role", failures
            )
            postures.append(report.get("role"))
            if report.get("role") == "active":
                promoted = report
                break
        evidence["preempt"] = {"watch": postures}
        if promoted is None:
            failures.append(
                "the promoted sibling never settled into the active "
                f"role — GET /role answers {report}"
            )
            raise Abort
        incumbent = pair.get(f"{duty_url}/role", "GET /role", failures)
        if incumbent.get("role") != "active":
            raise Inconclusive(
                "the preempted holder reported "
                f"{incumbent.get('role')!r} before its detection scan "
                "— the fenced-reporting window the raced admission "
                "needs never stood open; the pinned release predates "
                "the receipt-index-collision contract's staging"
            )
        sibling_index = next_receipt_index(
            standby_url, "GET /checkpoint", failures
        )
        evidence["indices"] = {
            "duty": index, "standby": sibling_index}
        if sibling_index != index:
            raise Inconclusive(
                "the promoted sibling's window mints at "
                f"{sibling_index} where the holder mints at {index} — "
                "its boundary fetch met a document past the coming "
                "admission; the split mint never formed and the pinned "
                "release predates the collision contract's staging"
            )
        digest_entries.append(
            {"phase": "preempt", "watch": postures, "window": "open"}
        )

        # Phase 3 — the raced admission on the still-field-owning
        # holder inside the promote/fence window: minted at the index
        # the sibling is about to mint from, then superseded at the
        # holder's fenced detection scan.
        status, raced = pair.request(
            f"{duty_url}/command",
            {
                "command": raced_command,
                "actor": RACED_ACTOR,
                "reason": REASON,
            },
        )
        evidence["raced_submission"] = {
            "status": status, "receipt": raced}
        if status != 200 or simulate.receipt_outcome(raced) != "accepted":
            failures.append(
                f"the raced admission answered {status} {raced}, "
                f"expected an accepted receipt at index {index}"
            )
            raise Abort
        if raced.get("actor") != RACED_ACTOR or \
                raced.get("reason") != REASON:
            raise Inconclusive(
                "the served receipt drops the declared actor/reason — "
                "the submission-record identity the collision audit "
                "correlates by; the pinned release predates the "
                "receipt-index-collision contract"
            )
        roles = []
        fenced = None
        for _ in range(WATCH_SCANS):
            pair.scan(duty_url, failures)
            report = pair.get(f"{duty_url}/role", "GET /role", failures)
            roles.append(report.get("role"))
            if report.get("role") == "standby":
                fenced = report
                break
        evidence["demote_watch"] = roles
        if fenced is None:
            failures.append(
                f"the preempted holder never demoted — its reported "
                f"role stayed {roles}"
            )
            raise Abort
        dig = {"phase": "race", "index": index, "watch": roles}
        digest_entries.append(dig)

        # Phase 4 — the colliding mint: a second receipted admission
        # on the new active, minting the same absolute index.
        status, minted = pair.request(
            f"{standby_url}/command",
            {
                "command": minted_command,
                "actor": MINTED_ACTOR,
                "reason": REASON,
            },
        )
        evidence["minted_submission"] = {
            "status": status, "receipt": minted}
        if status != 200 or simulate.receipt_outcome(minted) != "accepted":
            failures.append(
                f"the colliding admission answered {status} "
                f"{minted}, expected an accepted receipt"
            )
            raise Abort
        minted_index = next_receipt_index(
            standby_url, "GET /checkpoint", failures
        )
        evidence["minted_index"] = minted_index
        if minted_index != index:
            raise Inconclusive(
                f"the colliding admission minted at {minted_index} "
                f"where the raced one stands at {index} — the two "
                "lines never contended for one index, so the split "
                "mint the contract adjudicates never formed; the "
                "pinned release predates the collision contract"
            )
        digest_entries.append(
            {"phase": "mint", "index": minted_index}
        )

        # Phase 5 — the convergence: the new active applies its own
        # admission, then the demoted holder's tracking pull adopts
        # that window — the merge's re-home of the displaced receipt
        # past the window's high-water.
        postures = []
        applied = None
        for _ in range(SETTLE_SCANS):
            pair.scan(standby_url, failures)
            time.sleep(FETCH_SETTLE_S)
            rows = admission_receipts(
                served(standby_url, "GET /receipts", failures),
                minted_command, MINTED_ACTOR,
            )
            report = pair.get(
                f"{standby_url}/role", "GET /role", failures
            )
            postures.append(report.get("role"))
            if rows and not str(verdict(rows[-1])).startswith("accepted"):
                applied = rows[-1]
                break
        evidence["settle_watch"] = postures
        if applied is None:
            failures.append(
                "receipt-collision-failed: the new active never "
                f"applied its colliding admission — its served log "
                f"reads "
                f"{[verdict(r) for r in admission_receipts(served(standby_url, 'GET /receipts', failures), minted_command, MINTED_ACTOR)]}"
            )
            raise Abort
        holder_view = None
        for _ in range(RESOLVE_SCANS):
            pair.scan(duty_url, failures)
            report = pair.get(f"{duty_url}/role", "GET /role", failures)
            postures.append(report.get("role"))
            if claim_reclaim.tracking(report):
                holder_view = report
                break
        evidence["converge_watch"] = postures
        if holder_view is None:
            failures.append(
                "the demoted holder never reconverged to a promotable "
                f"verdict on the new active's stream — {postures}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "converge",
                "applied": verdict(applied),
                "holder": "tracking",
            }
        )

        # Phase 6 — the audit: every admitted command's terminal
        # verdict stays retrievable on the pair's served surface and
        # in its journals, the two submissions stand as distinct
        # records, and each peer recorded each admission once.
        observed = {
            "raced": {"served": {}, "journaled": {}, "durable": {}},
            "minted": {"served": {}, "journaled": {}, "durable": {}},
        }
        for key, command, actor in (
            ("raced", raced_command, RACED_ACTOR),
            ("minted", minted_command, MINTED_ACTOR),
        ):
            for name, url in peers.items():
                rows = admission_receipts(
                    served(url, "GET /receipts", failures),
                    command, actor,
                )
                observed[key]["served"][name] = (
                    verdict(rows[-1]) if rows else None
                )
                entries = pair.get(
                    f"{url}/journal", "GET /journal", failures
                )
                observed[key]["journaled"][name] = settle_rows(
                    entries, command, actor
                )
                path = (
                    rig.duty_files if name == duty_name
                    else rig.standby_files
                )["journal_file"]
                if not os.path.exists(path):
                    raise Inconclusive(
                        f"{name}'s declared journal file {path} does "
                        "not exist — the pinned release did not honor "
                        "the --journal-file persistence the durable "
                        "audit needs"
                    )
                observed[key]["durable"][name] = settle_rows(
                    journal_entries(path), command, actor
                )
        evidence["observed"] = observed
        fault = collision_fault(observed, tamper)
        if fault is not None:
            failures.append(fault[0] + ": " + fault[1])
            raise Abort
        # The displaced admission's preserved audit, read off the
        # served surface: the demoted holder serves it re-minted past
        # the contested index with its superseded verdict intact.
        raced_rows = admission_receipts(
            served(duty_url, "GET /receipts", failures),
            raced_command, RACED_ACTOR,
        )
        if not raced_rows:
            failures.append(
                "receipt-collision-failed: the demoted holder's "
                "served log carries no record of the raced admission "
                "after the merge re-homed it — only the successor's "
                "receipt stands at the contested index"
            )
            raise Abort
        if not str(verdict(raced_rows[-1])).startswith("superseded"):
            failures.append(
                "receipt-collision-failed: the re-minted raced "
                f"admission reads {verdict(raced_rows[-1])} rather "
                "than the fence's superseded verdict — the displaced "
                "record lost its terminal verdict"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "audit",
                "raced": observed["raced"]["served"],
                "minted": observed["minted"]["served"],
                "journaled": {
                    key: {
                        name: len(rows)
                        for name, rows
                        in observed[key]["journaled"].items()
                    }
                    for key in ("raced", "minted")
                },
            }
        )

        # Phase 7 — the launch roles.
        rig.promote(
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
            if duty_role.get("role") == "active" and \
                    claim_reclaim.tracking(standby_role):
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
        choices=["expect-displaced"],
        help="doctor the run — accept the displaced admission as "
        "served while only the successor's receipt stands at the "
        "contested index, the silent displacement the contract "
        "closed; the case must fail naming the diagnostic",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = collision_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                f"receipt-collision: the {args.tamper} case wanted "
                "the run to surface its named diagnostic — an "
                "inconclusive run offers the doctored case no evidence"
            )
            return 1
        eprint(f"receipt-collision: inconclusive — {inconclusive}")
        print(
            f"receipt-collision-digest inconclusive — {inconclusive}"
        )
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"receipt-collision: {line}")
        return 1
    for failure in failures:
        eprint(f"receipt-collision: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"receipt-collision: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored "
                "expectation"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"receipt-collision-digest {digest} — one raced admission "
        "superseded at the promote/fence window and a second minting "
        "the same absolute index both keep their retrievable terminal "
        "verdict on the pair's served surface, one command_settled "
        "per admission per peer journal, the launch roles restored"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())