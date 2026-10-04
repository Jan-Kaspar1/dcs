#!/usr/bin/env python3
"""The repromote-suspended-settle leg for the reference plant — the
consumer-side proof that a receipted command suspended `Accepted` at
a holder's fenced demote resolves when that same holder is
re-promoted inside the run: re-queued and settled exactly once at
its re-taken field-owning boundary, never parked `Accepted` on the
live active and never deferred to apply stale on a later successor's
promotion (WW-ENG-003, WW-LCM-001 — the #1050 re-promoted holder's
suspended-receipt settle contract the rig's
repromote-suspended-settle scenario pins in-workspace, mirrored at
the customer boundary; the gossip-window re-promote variant open
defect #708 owns is out of scope).

The demote-pending leg (`ci/legs/demote_pending.py`) proves a
pending command crossed by a documented switch settles exactly once
through the promotion-boundary carry — the successor's final-sync
fetch adorning the still-`Accepted` admission into its own log. The
suspended-alias-audit leg proves the adopted-window collision's
named adjudication; the resume-settle-once leg proves a restored
suspended receipt parks across a restart. None stages the
same-holder re-promote the #1050 fix owes a verdict: the suspended
receipt must re-queue on the holder it was suspended on, at the
boundary that holder re-takes — the tracked stream between demote
and re-promote covering the admission would make it the carry path
instead, so the leg's ordering is what distinguishes the contract.

The driven pair makes the ordering deterministic without the rig's
free-running polls: a peer's pulls and applies live only inside its
own `POST /scan`, so the leg stages the fenced-reporting window by
which peer it scans. The run:

- converges the declared pair to `tracking` through the pair leg's
  driven-tick loop and gates the contract surface — each peer's
  served checkpoint carrying the receipt window and admission
  counters the index correlation reads, plus the manifest's declared
  journal files the durable audit half needs;
- promotes the tracking sibling FIRST — its promotion-boundary
  final-sync fetch meets only the pre-admission checkpoint, and its
  unconditional claim preempts the incumbent under its standing
  active report, so the sibling's served window can never cover the
  admission the leg next lands;
- submits a receipted `write_value` on a declared writable internal
  `In` point — the same `p101-hand` target the force legs resolve —
  on the incumbent inside its fenced-but-still-reporting window: the
  admission mints `accepted` at the recorded index while the claim
  is already lost;
- drives the incumbent's detection scans until it settles `standby`
  — the fenced boundary suspending the pending receipt back to
  `Accepted` and demoting the holder in place — then asserts the
  admission still stands `accepted` at its index and the fenced
  image rolled the write back to baseline;
- reconverges the demoted peer to a promotable `tracking` verdict on
  the promoted sibling's checkpoint stream, then asserts the stream
  never covered the admission — the sibling's submission high-water
  still mints at the admission's own index and its log holds no
  receipt for it, while the holder's copy still stands `accepted`:
  the uncovered suspended tail the re-promote owes a verdict;
- re-promotes the original holder — its first field-owning scan
  re-queues the suspended tail and settles it — and drives the
  newly-demoted sibling's reconvergence, whose tracking pulls adopt
  the settled line;
- audits both peers' serving monitors and both declared durable
  journals: the point serves the command's value, `/receipts`
  carries the terminal `applied` verdict rather than a parked
  `accepted`, and exactly one `command_settled` per admission stands
  on each peer's journal — the re-promoted holder's own boundary
  plus the adopted record the demoted sibling journals
  reconverging;
- restores the pair's launch roles — the manifest-declared duty
  controller `active`, its standby `tracking` — which the
  re-promote already lands, so the restore is a holding pull train
  and the final role assertions.

The contract postdates the release line's v0.3.0 cut: where the
launched tooling predates it the run's own evidence is the
pre-contract shape — a checkpoint carrying no receipt window or
admission counters, a receipt dropping its declared
`actor`/`reason`, the fenced-reporting window never opening, a
demote settling or dropping rather than suspending the admission,
a tracked stream that covered or adjudicated it before the
re-promote, or a demoted peer that never reaches a promotable
verdict — and the leg reports
`repromote-suspended-settle-digest inconclusive` rather than
asserting until the manifest repins a release carrying the
contract.

Usage:

    repromote_suspended_settle.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `repromote-suspended-settle-digest <sha256>` line
prints — the check runs two passes and compares them
(`repromote-suspended-settle-nondeterministic`). A contract
violation reports `repromote-suspended-settle: …` lines on stderr
and exits 1 — the check's `repromote-suspended-settle-failed`.
`--tamper expect-parked` doctors the leg's own expectation to the
defect shape — asserting the suspended admission stays parked
`Accepted` past the re-promoted boundary — so the leg proves its
settle audit fires on the honest applied record rather than passing
an unexercised contract.
"""

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import force_carryover
import pair
import simulate
import takeover


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored case: a leg asserting the suspended admission may
# stay parked `Accepted` past the re-promoted holder's boundary —
# the defect the contract closed — must surface the named
# diagnostic on the honest applied record rather than passing an
# unexercised contract.
LEG = {
    "order": 490,
    "title": "the repromote-suspended-settle leg",
    "passes": "repromote-suspended-settle",
    "tampers": [
        {
            "name": "expect-parked",
            "passed": "an expect-parked case passed the repromote-suspended-settle leg",
            "missed": "the expect-parked case did not report its named diagnostic",
            "evidence": ["the doctored expectation wanted the suspended admission parked"],
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


# The driven-scan bounds each transition gets: the promoted
# sibling's settle into the field-owning role, the fenced demote's
# detection scans, the demoted peer's reconvergence to a promotable
# verdict, the re-promoted holder's settle, the newly-demoted
# sibling's adoption, and the launch-role hold train.
SETTLE_SCANS = 6
WATCH_SCANS = 6
RESOLVE_SCANS = 10
RESTORE_TICKS = 4

# The admission's declared identity — unique in both peers' receipt
# logs, the (command, actor) pair the settle audit correlates by.
ACTOR = "ci-repromote"
REASON = "repromote-suspended-settle"


def admission_hit(receipt, command, actor):
    """Whether a served or journaled receipt is the named admission —
    the (command, actor) pair is unique to its submission."""
    return (
        isinstance(receipt, dict)
        and receipt.get("command") == command
        and receipt.get("actor") == actor
    )


def admission_settles(entries, command, actor):
    """The `(seq, tick, outcome)` of a journal entry list's
    `command_settled` records answering the admission — served
    `GET /journal` entries and durable journal-file records alike."""
    found = []
    for entry in entries:
        settled = entry.get("event", {}).get("command_settled")
        if settled is None:
            continue
        if admission_hit(settled.get("receipt", {}), command, actor):
            found.append(
                (
                    entry["seq"],
                    entry["tick"],
                    simulate.receipt_outcome(settled["receipt"]),
                )
            )
    return found


def journal_entries(path):
    """A `--journal-file`'s entry records in file order — the durable
    half of the audit."""
    return [
        record
        for kind, record in pair.journal_records(path)
        if kind == "entry"
    ]


def receipt_window(url, what, failures):
    """The retained receipt tail and the absolute submission index of
    its first entry — the (log, high-water) pair the coverage audit
    correlates by, from the checkpoint's carried window and admission
    counters rather than the bare `GET /receipts` tail whose place in
    the sequence is unsaid."""
    checkpoint = pair.get(f"{url}/checkpoint", what, failures)
    receipts = checkpoint.get("receipts")
    attempts = (checkpoint.get("command_admission") or {}).get("attempts")
    if not isinstance(receipts, list) or not isinstance(attempts, int):
        raise Inconclusive(
            "the served checkpoint carries no receipt window or "
            "admission counters — the index correlation the contract "
            "stands on; the pinned release predates the "
            "repromote-suspended-settle contract"
        )
    return receipts, max(0, attempts - len(receipts))


def next_receipt_index(url, what, failures):
    """The absolute submission index the peer's next `POST /command`
    mints — the window's high-water `base + len(receipts)`."""
    receipts, base = receipt_window(url, what, failures)
    return base + len(receipts)


def index_receipt(url, index, what, failures):
    """The served receipt at absolute submission `index`, or None
    while the retained window does not cover it — the suspended
    admission's still-standing proof on its holder."""
    receipts, base = receipt_window(url, what, failures)
    position = index - base
    if 0 <= position < len(receipts):
        return receipts[position]
    return None


def admission_receipts(url, command, actor, what, failures):
    """The admission's entries in the peer's served `GET /receipts`
    log — the serving half of the audit."""
    receipts = pair.get(f"{url}/receipts", what, failures)
    return [
        entry for entry in receipts if admission_hit(entry, command, actor)
    ]


def tracking(report):
    """Whether a served RoleReport carries `standby` under the
    `tracking` sync state — the promotable verdict the demoted
    peer's reconvergence owes."""
    sync = report.get("sync") if isinstance(report, dict) else None
    return report.get("role") == "standby" and (
        isinstance(sync, dict) and "tracking" in sync
    )


def sync_posture(report):
    """The sync variant a served RoleReport carries — the posture
    name the digest records; the variant's payload is kept out of
    the digest (a `degraded` detail names the pull's target
    address)."""
    sync = report.get("sync") if isinstance(report, dict) else None
    return next(iter(sync), "unsynchronized") if isinstance(sync, dict) else sync


def repromote_settle_pass(args, tamper):
    """The repromote-suspended-settle run: converge, preempt with the
    sibling's promote, admit inside the fenced-reporting window,
    demote the holder suspending the receipt, reconverge it
    uncovered on the promoted sibling's stream, re-promote the
    holder, audit both monitors and both durable journals for the
    exactly-once settle, and restore the launch roles. Returns
    `(digest_entries, evidence, failures)`; raises `Inconclusive`
    where the pinned release predates the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "repromote-suspended-settle leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    duty_name, standby_name = duty_decl["name"], standby_decl["name"]
    with open(args.model) as handle:
        model = json.load(handle)
    point = force_carryover.force_target(model)
    if point is None:
        raise Abort(
            f"the emitted model declares no writable In point at "
            f"{force_carryover.FORCE_SIGNAL} — the repromote-suspended-settle "
            "leg has nothing to suspend"
        )

    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url

        # Phase 1 — convergence: the pair leg's driven-tick loop, the
        # tracking peer scanned first so each pull applies the owner's
        # latest checkpoint and the peers rest at the same tick with
        # identical images.
        converged = rig.converge(failures)
        owner = converged["owner"]
        evidence["converged"] = converged["ticks"][-1]
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )

        # The contract surface: both peers' checkpoints must carry
        # the receipt window and admission counters the index
        # correlation reads, and the manifest must declare the
        # journal files the durable audit reads. A surface absent is
        # the release predating the contract, never a violation of
        # it.
        for name, url in (
            (duty_name, duty_url),
            (standby_name, standby_url),
        ):
            receipt_window(url, f"GET /checkpoint on {name}", failures)
        if (
            rig.duty_files.get("journal_file") is None
            or rig.standby_files.get("journal_file") is None
        ):
            raise Inconclusive(
                "the manifest's pair declares no journal_file "
                "persistence — the durable half of the settle audit "
                "is absent"
            )
        baseline = simulate.snapshot_point(owner, point)
        if baseline is None or "bool" not in baseline:
            raise Abort(
                f"the suspension target point {point} serves "
                f"{baseline} — a bool baseline the leg can flip is "
                "required"
            )
        value = {"bool": not baseline["bool"]}
        command = takeover.write_value(point, value["bool"])

        # The admission's index: the converged peers must agree on
        # the submission high-water — the adopted-window coverage
        # audit reads the sibling's mint index against it.
        index = next_receipt_index(duty_url, "GET /checkpoint", failures)
        sibling_index = next_receipt_index(
            standby_url, "GET /checkpoint", failures
        )
        evidence["indices"] = {"duty": index, "standby": sibling_index}
        if index != sibling_index:
            failures.append(
                "the converged peers disagree on the submission "
                f"high-water — the tracking peer mints at "
                f"{sibling_index} where the owner mints at {index} — "
                "the converged line never stood"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "gate",
                "index": index,
                "point": point,
                "persistence": "declared",
            }
        )

        # Phase 2 — the preempting promote lands before the
        # admission: the sibling's promotion-boundary final-sync
        # fetch meets only the pre-admission checkpoint, so its
        # served window can never cover the coming command, and its
        # unconditional claim preempts the incumbent under its
        # standing active report — the fenced demote the suspension
        # rides. The incumbent is never scanned, so the
        # fenced-but-still-reporting window stands open at the
        # submission.
        preempt = rig.promote(
            standby_url, failures, "the tracking sibling"
        )
        postures = []
        promoted = None
        for _ in range(SETTLE_SCANS):
            pair.scan(standby_url, failures)
            report = pair.get(
                f"{standby_url}/role", "GET /role", failures
            )
            postures.append(report.get("role"))
            if report.get("role") == "active":
                promoted = report
                break
        evidence["preempt"] = {"promote": preempt, "watch": postures}
        if promoted is None:
            failures.append(
                "the promoted sibling never settled into the active "
                f"role — GET /role answers {report}"
            )
            raise Abort
        incumbent = pair.get(f"{duty_url}/role", "GET /role", failures)
        evidence["incumbent_window"] = incumbent.get("role")
        if incumbent.get("role") != "active":
            raise Inconclusive(
                "the incumbent reported "
                f"{incumbent.get('role')!r} before its detection "
                "scan — the fenced-reporting window the suspension "
                "needs never stood open; the pinned release predates "
                "the contract's staging"
            )
        digest_entries.append(
            {
                "phase": "preempt",
                "promote": preempt,
                "watch": postures,
                "window": "fenced-reporting",
            }
        )

        # Phase 3 — the suspended admission: the receipted write on
        # the still-reporting-active incumbent inside the fenced
        # window — admitted pending, then never scanned, so the
        # detection scan's fenced demote suspends it back to
        # `Accepted`.
        status, suspended = pair.request(
            f"{duty_url}/command",
            {
                "command": command,
                "actor": ACTOR,
                "reason": REASON,
            },
        )
        evidence["suspended_submission"] = {
            "status": status,
            "receipt": suspended,
        }
        if status != 200 or simulate.receipt_outcome(suspended) != "accepted":
            failures.append(
                f"the fenced-window submission answered {status} "
                f"{suspended}, expected an accepted receipt at index "
                f"{index}"
            )
            raise Abort
        if suspended.get("actor") != ACTOR or suspended.get("reason") != REASON:
            raise Inconclusive(
                "the served receipt drops the declared actor/reason "
                "— the submission-record identity the settle audit "
                "correlates by; the pinned release predates the "
                "repromote-suspended-settle contract"
            )
        digest_entries.append(
            {
                "phase": "suspend",
                "index": index,
                "point": point,
                "receipt": suspended,
            }
        )

        # Phase 4 — the demote-in-place watch: the superseded owner's
        # detection scans walk demoting to standby, the fenced
        # boundary suspending the pending receipt back to `Accepted`
        # and rolling its write out of the fenced image. A boundary
        # that settled or dropped the admission instead of
        # suspending it is the pre-contract shape.
        roles = []
        settled = None
        fenced_scan = None
        for _ in range(WATCH_SCANS):
            fenced_scan = pair.scan(duty_url, failures)
            report = pair.get(f"{duty_url}/role", "GET /role", failures)
            roles.append(report.get("role"))
            if report.get("role") == "standby":
                settled = report
                break
        evidence["demote_watch"] = roles
        if settled is None:
            failures.append(
                "the superseded owner never demoted — its reported "
                f"role stayed {roles}"
            )
            raise Abort
        standing = index_receipt(
            duty_url, index, "GET /checkpoint", failures
        )
        evidence["suspended_standing"] = standing
        if not admission_hit(standing, command, ACTOR):
            raise Inconclusive(
                f"the suspended admission's served receipt reads "
                f"{standing} at index {index} after the demote — "
                "the admission never stood suspended; the pinned "
                "release predates the suspended-entry contract"
            )
        if simulate.receipt_outcome(standing) != "accepted":
            raise Inconclusive(
                f"the demote settled the admission "
                f"{simulate.receipt_outcome(standing)} instead of "
                "suspending it — the pinned release predates the "
                "suspended-entry contract"
            )
        fenced_value = simulate.snapshot_point(fenced_scan, point)
        evidence["fenced_value"] = fenced_value
        if fenced_value != baseline:
            failures.append(
                f"the demoted holder's fenced image reads "
                f"{fenced_value} for point {point}, expected the "
                f"baseline {baseline} — the suspension rolled no "
                "write back"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "demote",
                "watch": roles,
                "standing": "accepted",
                "image": "baseline",
            }
        )

        # Phase 5 — the reconvergence: the demoted peer resolves the
        # promoted sibling through the field's declared claim
        # monitor and pulls its checkpoint stream to a promotable
        # verdict. The sibling is scanned ahead each round so its
        # served document stays strictly ahead of the demoted run's.
        postures = []
        reconverged = None
        for _ in range(RESOLVE_SCANS):
            pair.scan(standby_url, failures)
            pair.scan(duty_url, failures)
            report = pair.get(f"{duty_url}/role", "GET /role", failures)
            postures.append(sync_posture(report))
            if tracking(report):
                reconverged = report
                break
        evidence["reconverge"] = {"postures": postures}
        if reconverged is None:
            failures.append(
                "the demoted peer never reconverged to a promotable "
                "verdict on the promoted sibling's checkpoint "
                f"stream — postures {postures}"
            )
            raise Abort

        # The uncovered evidence: the promoted sibling's adopted
        # window still mints at the admission's index — its stream
        # never covered the receipt — and the holder's copy still
        # parks `Accepted` after reconverging. Either breaking means
        # the tracked stream adjudicated the admission, not the
        # re-promote this leg exercises.
        sibling_index = next_receipt_index(
            standby_url, "GET /checkpoint", failures
        )
        sibling_logged = admission_receipts(
            standby_url, command, ACTOR, "GET /receipts", failures
        )
        holder_served = index_receipt(
            duty_url, index, "GET /checkpoint", failures
        )
        evidence["pre_repromote"] = {
            "sibling_next_index": sibling_index,
            "sibling_receipts": sibling_logged,
            "holder_receipt": holder_served,
        }
        if sibling_logged or sibling_index > index:
            raise Inconclusive(
                "the promoted sibling's window covered the staged "
                f"admission — next index {sibling_index}, logged "
                f"{sibling_logged} — its final-sync carried the "
                "receipt after all: the re-promote contract never "
                "engaged"
            )
        if holder_served is None or not admission_hit(
            holder_served, command, ACTOR
        ):
            raise Inconclusive(
                "the staged admission vanished across the "
                "reconvergence — the demoted peer exposes no "
                "suspended-entry shape the contract owes; the "
                "pinned release predates it"
            )
        if simulate.receipt_outcome(holder_served) != "accepted":
            raise Inconclusive(
                "the staged admission settled "
                f"{simulate.receipt_outcome(holder_served)} across "
                "the reconvergence — the tracked stream adjudicated "
                "it before the re-promote"
            )
        digest_entries.append(
            {
                "phase": "reconverge",
                "postures": postures,
                "window": "uncovered",
            }
        )

        # Phase 6 — the re-promote: the reconverged holder takes the
        # field back — its first field-owning scan re-queues the
        # suspended tail and settles it once.
        repromote = rig.promote(
            duty_url, failures, "the reconverged holder"
        )
        repromoted = None
        settle_roles = []
        for _ in range(SETTLE_SCANS):
            pair.scan(duty_url, failures)
            report = pair.get(f"{duty_url}/role", "GET /role", failures)
            settle_roles.append(report.get("role"))
            if report.get("role") == "active":
                repromoted = report
                break
        evidence["repromote"] = {"promote": repromote, "watch": settle_roles}
        if repromoted is None:
            failures.append(
                "the re-promoted holder never settled into the "
                f"active role — GET /role answers {report}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "repromote",
                "promote": repromote,
                "watch": settle_roles,
            }
        )

        # Phase 7 — the sibling's demote and reconvergence: the
        # preempted incumbent's detection scans walk it demoting to
        # standby, its tracking pulls adopting the settled line —
        # the adopted `command_settled` record the pair's
        # once-each-per-peer audit expects on it. The re-promoted
        # owner is scanned ahead each round so its served document
        # stays strictly ahead.
        postures = []
        sibling = None
        for _ in range(RESOLVE_SCANS):
            pair.scan(duty_url, failures)
            pair.scan(standby_url, failures)
            report = pair.get(
                f"{standby_url}/role", "GET /role", failures
            )
            postures.append(sync_posture(report))
            if tracking(report):
                sibling = report
                break
        evidence["sibling_reconverge"] = {"postures": postures}
        if sibling is None:
            failures.append(
                "the newly-demoted sibling never reconverged "
                "tracking behind the re-promoted holder — postures "
                f"{postures}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "sibling",
                "postures": postures,
            }
        )

        # Phase 8 — the served audit: both monitors' journals and
        # receipt logs, the served images, and the adopted one-log
        # proof. The contract: exactly one `command_settled` per
        # admission on each peer — `applied`, the re-promoted
        # holder's own boundary plus the demoted sibling's adopted
        # record — the served log carrying the terminal verdict
        # rather than a parked `accepted`, and the point serving the
        # command's value. Under `expect-parked` the leg doctors
        # its expectation to the defect shape — the admission still
        # `Accepted` past the boundary.
        journals = {
            name: pair.get(f"{url}/journal", "GET /journal", failures)
            for name, url in ((duty_name, duty_url), (standby_name, standby_url))
        }
        settles = {
            name: admission_settles(journal, command, ACTOR)
            for name, journal in journals.items()
        }
        logged = {
            name: admission_receipts(
                url, command, ACTOR, "GET /receipts", failures
            )
            for name, url in (
                (duty_name, duty_url),
                (standby_name, standby_url),
            )
        }
        images = {
            name: simulate.snapshot_point(
                pair.get(f"{url}/snapshot", "GET /snapshot", failures),
                point,
            )
            for name, url in (
                (duty_name, duty_url),
                (standby_name, standby_url),
            )
        }
        evidence["audit"] = {
            "settles": settles,
            "logged": logged,
            "images": images,
        }
        if tamper == "expect-parked":
            # The doctored expectation — the leg asserts the
            # suspended admission stays parked `Accepted` past the
            # re-promoted holder's boundary, the defect the
            # contract closed. The honest applied record must fail
            # it, naming the settlement the run stood on.
            failures.append(
                "the doctored expectation wanted the suspended "
                "admission parked Accepted past the re-promoted "
                f"boundary — {ACTOR} journaled "
                f"{settles[duty_name]} on its holder and logged "
                f"{[simulate.receipt_outcome(r) for r in logged[duty_name]]}"
            )
            raise Abort
        outcomes = {
            outcome
            for entries in settles.values()
            for _seq, _tick, outcome in entries
        }
        for name, expected in (
            (duty_name, "the re-promoted holder's own boundary"),
            (standby_name, "the demoted sibling's adopted record"),
        ):
            if len(settles[name]) != 1:
                failures.append(
                    f"{name} journaled {len(settles[name])} "
                    f"command_settled records for the admission — "
                    f"the contract owes exactly one there: "
                    f"{expected}"
                )
            elif settles[name][0][2] != "applied":
                failures.append(
                    f"{name}'s settle for the admission is "
                    f"{settles[name][0][2]} — the restored suspended "
                    "receipt owes applied"
                )
        if outcomes and outcomes != {"applied"}:
            failures.append(
                f"the admission journaled {sorted(outcomes)} across "
                "the pair — one admission, never more than one "
                "terminal outcome"
            )
        for name in (duty_name, standby_name):
            parked = [
                receipt
                for receipt in logged[name]
                if simulate.receipt_outcome(receipt) == "accepted"
            ]
            if parked:
                failures.append(
                    f"the admission is still parked Accepted on "
                    f"{name} — the re-promoted holder's boundary "
                    "never re-queued it"
                )
            terminal = [
                receipt
                for receipt in logged[name]
                if simulate.receipt_outcome(receipt) != "accepted"
            ]
            if len(terminal) != 1 or (
                terminal
                and simulate.receipt_outcome(terminal[0]) != "applied"
            ):
                failures.append(
                    f"{name}'s served log carries "
                    f"{[simulate.receipt_outcome(r) for r in logged[name]]} "
                    "for the admission — the terminal verdict owed "
                    "is one applied"
                )
            if images[name] != value:
                failures.append(
                    f"{name}'s image reads {images[name]} for point "
                    f"{point}, expected the command's value {value} "
                    "— a settlement without the command's "
                    "application"
                )
        receipts_duty = pair.get(
            f"{duty_url}/receipts", "GET /receipts", failures
        )
        receipts_standby = pair.get(
            f"{standby_url}/receipts", "GET /receipts", failures
        )
        if receipts_duty != receipts_standby:
            failures.append(
                "the peers' receipt logs diverged across the "
                "re-promote — the adopted audit is not one log"
            )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "audit",
                "settles": settles,
                "logged": {
                    name: [
                        simulate.receipt_outcome(receipt)
                        for receipt in entries
                    ]
                    for name, entries in logged.items()
                },
                "images": images,
            }
        )

        # Phase 9 — the durable half: each peer's declared journal
        # file carries the same one settle per admission the serving
        # monitor did — the settlement reaching the durable record,
        # never a served-only boundary.
        durable = {}
        for name, files in (
            (duty_name, rig.duty_files),
            (standby_name, rig.standby_files),
        ):
            journal_path = files["journal_file"]
            if not os.path.exists(journal_path):
                failures.append(
                    f"{name}'s declared journal file {journal_path} "
                    "does not exist — the --journal-file flag was "
                    "not honored"
                )
                raise Abort
            durable[name] = admission_settles(
                journal_entries(journal_path), command, ACTOR
            )
        for name in (duty_name, standby_name):
            if durable[name] != settles[name]:
                failures.append(
                    f"{name}'s durable journal carries "
                    f"{durable[name]} for the admission where its "
                    f"served journal carries {settles[name]} — the "
                    "durable audit is not the served audit"
                )
        if failures:
            raise Abort
        digest_entries.append({"phase": "durable", "durable": durable})

        # Phase 10 — the launch roles: the re-promote already landed
        # the manifest's arrangement — the duty controller `active`,
        # its standby `tracking`. The hold train asserts the pair
        # keeps the layout across repeated driven pulls, each tick
        # proving identical images.
        restore_ticks = []
        for _ in range(RESTORE_TICKS):
            _tracked, owner = rig.tick(
                standby_url,
                duty_url,
                failures,
                diverged="the restored pair's images diverged at "
                "tick {tick} — the launch-role layout did not hold",
            )
            restore_ticks.append(owner["tick"])
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        standby_role = pair.get(
            f"{standby_url}/role", "GET /role", failures
        )
        if duty_role.get("role") != "active":
            failures.append(
                f"the re-promoted field owner reports "
                f"{duty_role.get('role')!r}, expected active — the "
                "pair was not left in its launch roles"
            )
        if not tracking(standby_role):
            failures.append(
                "the demoted sibling never settled tracking behind "
                f"the re-promoted holder — GET /role answers "
                f"{standby_role}"
            )
        if failures:
            raise Abort
        evidence["restored_at"] = restore_ticks[-1]
        digest_entries.append(
            {
                "phase": "restore",
                "ticks": restore_ticks,
                "duty_role": duty_role,
                "standby_role": standby_role,
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
        choices=["expect-parked"],
        help="doctor the leg's expectation to the defect shape — the "
        "pass must fail naming the settlement the honest run stood "
        "on",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = repromote_settle_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "repromote-suspended-settle: the doctored "
                "expectation wanted the suspended admission parked "
                "— an inconclusive run offers the doctored case no "
                "evidence"
            )
            return 1
        eprint(f"repromote-suspended-settle: inconclusive — {inconclusive}")
        print(
            f"repromote-suspended-settle-digest inconclusive — "
            f"{inconclusive}"
        )
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"repromote-suspended-settle: {line}")
        return 1
    for failure in failures:
        eprint(f"repromote-suspended-settle: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"repromote-suspended-settle: the {args.tamper} case "
                "passed silently — the leg never noticed the "
                "doctored expectation"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"repromote-suspended-settle-digest {digest} — tracking by "
        f"tick {evidence['converged']}, the suspended admission "
        f"staged at index {evidence['indices']['duty']} inside the "
        "fenced-reporting window, re-promoted and settled applied "
        "once on each peer's journal — durable included — the point "
        "serving the command's value, launch roles restored at tick "
        f"{evidence['restored_at']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
