#!/usr/bin/env python3
"""The quiesced-standby-settle leg for the reference plant — the
consumer-side proof that a tracking standby's quiesced scans never
settle an adopted pending command on its gated image, and that a
command carried pending across a promotion resolves exactly once at
its true boundary (WW-ENG-003, WW-LCM-001 — the carried-receipt
contract the rig's quiesced-standby-settle scenario pins on the
simulated QA rig, mirrored at the customer boundary on the
manifest-declared redundant pair).

The defect the contract closed: a tracking standby's quiesced scan
applied the adopted receipt on its staged image, minted a phantom
settled verdict that journaled before any real boundary, and was
never re-executed on the live line. The contract: while the line
still owes the admission a verdict, the gated image takes nothing —
the serving monitor carries no terminal receipt, and no
`command_settled` journals ahead of the boundary; at the promotion
boundary the carried command resolves exactly once, the adopted
record carrying the line's applied verdict verbatim, never a fresh
local mint.

The rig leg needs a third, driven controller to hold a standby
quiesced — a paced standby's pulls are continuous, so its pending
window is one apply boundary wide and unobservable. The deployed
pair is already driven — every pull the declared standby performs
happens inside its own `POST /scan` — so the manifest's tracking
member is itself the quiesced peer, and the pending window is
deterministic: the field owner applies a pending admission only at
its own scan boundary, which the leg simply never requests until
the hold is over. Two details of the driven rig shape the staging:

- The standby's run clock never rewinds: every quiesced scan ticks
  it once while the owner's frozen checkpoint cannot land ahead, so
  the standby finishes the hold strictly ahead of the owner's tick.
  A promote issued in that state makes the promotion boundary's
  final-sync fetch return a checkpoint stale by the standby's
  clock — the carry path that leaves the covered still-`Accepted`
  receipt to settle on the promoted peer's own first scan — rather
  than the verbatim adoption the contract's carried-record wording
  stands on. The paced pair this mirrors never presents that
  layout (the active's clock always leads), so the leg scans the
  owner ahead of the standby's run tick once the line's boundary
  has journaled — the fetched checkpoint then lands fresh and the
  adopted settled record carries the line's own apply tick.
- The fetch pipeline applies the checkpoint the *previous* scan's
  poll requested, so which scan first adopts the pending receipt
  depends on whether a pre-submission fetch was still in flight.
  The adoption loop pads the standby's quiesced scans to a fixed
  total so the run's tick ladder — and the digest — never shifts
  with fetch timing.

The run:

- converges the declared pair to `tracking` through the pair leg's
  driven-tick loop and gates the contract surface — each peer's
  served checkpoint carrying the receipt window and admission
  counters the audit correlates by, and the manifest declaring the
  journal files the durable half needs;
- submits a receipted `write_value` on a declared writable internal
  `In` point — the same `p101-hand` target the force legs resolve,
  gated inert downstream while the pump stands in auto — on the
  field owner's `POST /command`, asserting the `accepted` receipt
  echoes its declared actor and reason, then drives no owner scan:
  the admission stands pending on the owner's served checkpoint;
- drives the tracking standby's scans until its pull adopts the
  still-pending receipt, then holds the pair across a fixed count
  of further quiesced standby scans while the owner stays
  unscanned — the quiesced window the gated-image audit reads;
- asserts through the standby's serving monitor and its declared
  durable journal that no settle verdict was minted on the gated
  image: the adopted receipt still `accepted`, no `command_settled`
  on the served or durable journal, the staged image still reading
  the pre-command baseline, and the pair still in its settled
  tracking layout;
- scans the field owner past the standby's run clock — its first
  scan's own apply boundary journals the line's `command_settled`,
  the rest buy the fresh final-sync fetch — then promotes the
  quiesced standby, whose promotion boundary adopts the line's
  settled record verbatim;
- audits both peers' serving monitors and both declared durable
  journals: exactly one `command_settled` for the admission on each
  peer — `applied`, carrying the line's own apply tick — one
  terminal `applied` receipt in each adopted log, the point serving
  the command's value on both images, and the peers' receipt logs
  one identical record;
- restores the pair's launch roles — the preempted duty member's
  scans fencing it demoting to standby tracking the announced
  successor, the promoted peer's documented demote releasing the
  field, the duty member's promote reclaiming it, and the standby
  reconverged `tracking` — for the legs behind this one.

The contract postdates the earliest pins: where the pinned release
predates it the run's own evidence is the pre-contract shape — a
checkpoint carrying no receipt window or admission counters, a
served receipt dropping the declared `actor`/`reason`, the
manifest's pair declaring no journal files, or a tracking pull that
never adopts a still-pending admission — and the leg reports
`quiesced-standby-settle-digest inconclusive` rather than asserting
until the manifest repins a release carrying the contract.

Usage:

    quiesced_standby_settle.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `quiesced-standby-settle-digest <sha256>` line
prints — the check runs two passes and compares them
(`quiesced-standby-settle-nondeterministic`). A contract violation
reports `quiesced-standby-settle: …` lines on stderr and exits 1 —
the check's `quiesced-settle-failed`; a duplicated or divergent
resolution names `quiesced-settle-nondeterministic` in its own
line. `--tamper planted-phantom` doctors the pending-window audit
with a journaled settle on the gated image and `--tamper
unresolved-boundary` withholds the owner's apply scan — each a leg
asserting a settle shape the run never produced, surfacing the
named diagnostic (`quiesced-standby-settle-unchecked`).
"""

import argparse
import hashlib
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
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored cases: a journaled settle planted into the quiesced
# standby's pending-window audit — the phantom the contract forbids
# — and a boundary the owner never reached while the leg asserts
# its resolution — each must surface the named diagnostic on the
# honest run rather than passing an unexercised audit.
LEG = {
    "order": 590,
    "title": "the quiesced-standby-settle leg",
    "passes": "quiesced-standby-settle",
    "failed": "quiesced-settle-failed",
    "tampers": [
        {
            "name": "planted-phantom",
            "passed": "a planted-phantom case passed the quiesced-standby-settle leg",
            "missed": "the planted-phantom case did not report its named diagnostic",
            "evidence": ["a phantom settle"],
        },
        {
            "name": "unresolved-boundary",
            "passed": "an unresolved-boundary case passed the quiesced-standby-settle leg",
            "missed": "the unresolved-boundary case did not report its named diagnostic",
            "evidence": ["no true boundary"],
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
# before the next driven scan consumes it — the puller's
# one-fetch-per-poll cadence — and the driven-scan bounds each phase
# runs: the adopting pull's bound and the fixed quiesced window's
# total, the promoted peer's settle into the active role, the
# demoted peer's reconvergence walk, and the launch-role hold train.
FETCH_SETTLE_S = 0.3
ADOPT_SCANS = 6
HOLD_SCANS = 3
SETTLE_SCANS = 6
RESOLVE_SCANS = 10
RESTORE_TICKS = 4

# The admission's declared identity — unique in both peers' receipt
# logs, the (command, actor) pair the settle audit correlates by.
ACTOR = "ci-quiesced-standby"
REASON = "quiesced-standby-settle"


def applied_tick(receipt):
    """The apply tick a settled `applied` receipt carries, or None —
    the line's boundary stamp an adopted record must carry
    verbatim."""
    applied = ((receipt or {}).get("outcome") or {}).get("applied") or {}
    tick = applied.get("tick")
    return tick if isinstance(tick, int) and not isinstance(tick, bool) else None


def admission_hit(receipt, command):
    """Whether a served or journaled receipt is the leg's admission —
    the (command, actor) pair is unique to its submission."""
    return (
        isinstance(receipt, dict)
        and receipt.get("command") == command
        and receipt.get("actor") == ACTOR
    )


def admission_receipts(receipts, command):
    """The admission's entries in a served `GET /receipts` list."""
    return [entry for entry in receipts if admission_hit(entry, command)]


def admission_settles(journal, command):
    """The `(seq, tick, receipt)` triples of a journal's
    `command_settled` entries answering the admission — a served
    `GET /journal` entry list and a durable `--journal-file`'s entry
    records share the shape."""
    found = []
    for entry in journal:
        settled = entry.get("event", {}).get("command_settled")
        if settled is None:
            continue
        if admission_hit(settled.get("receipt"), command):
            found.append((entry["seq"], entry["tick"], settled["receipt"]))
    return found


def settle_rows(settles):
    """The digest-stable `(seq, tick, outcome)` view of an
    `admission_settles` list."""
    return [
        (seq, tick, simulate.receipt_outcome(receipt))
        for seq, tick, receipt in settles
    ]


def journal_entries(path):
    """A `--journal-file`'s entry records in file order — the durable
    half of the audit."""
    return [
        record
        for kind, record in pair.journal_records(path)
        if kind == "entry"
    ]


def receipt_window(url, what, failures):
    """The contract surface the audit correlates by: the served
    checkpoint's receipt window beside its admission counters.
    Absent, the pinned release predates the contract."""
    checkpoint = pair.get(f"{url}/checkpoint", what, failures)
    receipts = checkpoint.get("receipts")
    attempts = (checkpoint.get("command_admission") or {}).get("attempts")
    if not isinstance(receipts, list) or not isinstance(attempts, int):
        raise Inconclusive(
            "the served checkpoint carries no receipt window or "
            "admission counters — the pinned release predates the "
            "quiesced-standby-settle contract"
        )
    return receipts


def quiesced_settle_pass(args, tamper):
    """The quiesced-standby-settle run: converge, admit the carried
    command on the owner, adopt it still-pending on the standby's
    pull, hold the pair across quiesced scans auditing the gated
    image, settle the line at the owner's own scan, promote and
    audit the boundary's exactly-once resolution, and restore the
    launch roles. Returns `(digest_entries, evidence, failures)`;
    raises `Inconclusive` where the pinned release predates the
    contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "quiesced-standby-settle leg has nothing to exercise"
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
            "quiesced-standby-settle leg has nothing to carry"
        )

    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url

        # Phase 1 — convergence: the pair leg's driven-tick loop, the
        # tracking peer scanned first so each pull applies the owner's
        # latest checkpoint and the peers rest identical.
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
        # the receipt window and admission counters the audit
        # correlates by, and the manifest must declare the journal
        # files the durable half reads. A surface absent is the
        # release predating the contract, never a violation of it.
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
            failures.append(
                f"the carried admission's target point {point} serves "
                f"{baseline} — a bool baseline the leg can flip is "
                "required"
            )
            raise Abort
        value = {"bool": not baseline["bool"]}
        command = takeover.write_value(point, value["bool"])
        digest_entries.append(
            {"phase": "gate", "point": point, "persistence": "declared"}
        )

        # Phase 2 — the carried admission: the receipted write lands
        # pending on the field owner — never scanned, so its own
        # apply boundary stays closed through the whole hold.
        status, receipt = pair.request(
            f"{duty_url}/command",
            {"command": command, "actor": ACTOR, "reason": REASON},
        )
        if status != 200 or simulate.receipt_outcome(receipt) != "accepted":
            failures.append(
                f"the carried admission answered {status} {receipt}, "
                "expected an accepted receipt"
            )
            raise Abort
        if receipt.get("actor") != ACTOR or receipt.get("reason") != REASON:
            raise Inconclusive(
                "the served receipt drops the declared actor/reason "
                "— the submission-record identity the settle audit "
                "correlates by; the pinned release predates the "
                "quiesced-standby-settle contract"
            )
        digest_entries.append(
            {
                "phase": "admit",
                "point": point,
                "baseline": baseline,
                "command": command,
                "receipt": receipt,
            }
        )

        # Phase 3 — the adopting pull and the quiesced hold: the
        # tracking standby's scans request the checkpoint fetch the
        # following scan applies; inside the pending window the
        # adoption lands still `Accepted` — the carried command the
        # hold audits. The loop's early exit is padded back to the
        # fixed total: the adoption may land a scan early when the
        # fetch pipeline was still draining at the submission, and
        # the run's tick ladder — the digest's domain — must not
        # shift with the pull's timing.
        adopted = None
        used = 0
        for _ in range(ADOPT_SCANS):
            pair.scan(standby_url, failures)
            time.sleep(FETCH_SETTLE_S)
            used += 1
            matches = admission_receipts(
                pair.get(
                    f"{standby_url}/receipts", "GET /receipts", failures
                ),
                command,
            )
            if matches:
                adopted = matches[-1]
                break
        if adopted is None:
            raise Inconclusive(
                "the tracking standby's pulls never adopted the "
                "still-pending admission — the checkpoint carries no "
                "pending receipts; the pinned release predates the "
                "carried pending-receipt contract"
            )
        for _ in range(ADOPT_SCANS - used + HOLD_SCANS):
            pair.scan(standby_url, failures)
            time.sleep(FETCH_SETTLE_S)
        evidence["adopted"] = adopted
        digest_entries.append({"phase": "adopt", "receipt": adopted})

        # Phase 4 — the quiesced window's audit: while the owner
        # stays unscanned the standby holds the adopted still-pending
        # receipt across every quiesced scan above. The contract owes
        # nothing on the gated image while the line still owes the
        # admission a verdict: no terminal receipt served, no
        # `command_settled` journaled — served or durable — the
        # pending write never applied, the pair still in its settled
        # tracking layout. Under `planted-phantom` the leg doctors
        # the audit window — a journaled settle on the gated image
        # the honest run never mints — and the audit must name it.
        held_logged = admission_receipts(
            pair.get(f"{standby_url}/receipts", "GET /receipts", failures),
            command,
        )
        held_served = admission_settles(
            pair.get(f"{standby_url}/journal", "GET /journal", failures),
            command,
        )
        standby_journal = rig.standby_files["journal_file"]
        held_durable = (
            admission_settles(journal_entries(standby_journal), command)
            if os.path.exists(standby_journal)
            else []
        )
        held_scan = pair.get(
            f"{standby_url}/snapshot", "GET /snapshot", failures
        )
        held_image = simulate.snapshot_point(held_scan, point)
        held_duty = pair.get(f"{duty_url}/role", "GET /role", failures)
        held_standby = pair.get(
            f"{standby_url}/role", "GET /role", failures
        )
        if tamper == "planted-phantom":
            held_served.append(
                (
                    0,
                    0,
                    {
                        "command": command,
                        "actor": ACTOR,
                        "reason": REASON,
                        "outcome": {"applied": {"tick": 0}},
                    },
                )
            )
        held = {
            "receipt": held_logged[-1] if held_logged else None,
            "journaled": settle_rows(held_served),
            "durable": settle_rows(held_durable),
            "image": held_image,
            "pair": held_duty.get("role") == "active"
            and claim_reclaim.tracking(held_standby),
        }
        evidence["held"] = held
        label = f"the carried admission {ACTOR} (point {point})"
        if held["receipt"] is None:
            failures.append(
                f"quiesced-settle-failed: {label} vanished from the "
                "quiesced standby's adopted receipt log — the carried "
                "command was dropped unaudited"
            )
        elif simulate.receipt_outcome(held["receipt"]) != "accepted":
            failures.append(
                f"quiesced-settle-failed: {label} reads "
                f"{simulate.receipt_outcome(held['receipt'])} on the "
                "quiesced standby's serving monitor — a terminal "
                "verdict minted on the gated image while the line "
                "still owes it one"
            )
        for name, settles in (
            ("served journal", held_served),
            ("durable journal", held_durable),
        ):
            if settles:
                failures.append(
                    f"quiesced-settle-failed: {label} shows "
                    f"{len(settles)} command_settled records on the "
                    f"quiesced standby's {name} ahead of the "
                    f"boundary: "
                    f"{[simulate.receipt_outcome(receipt) for _s, _t, receipt in settles]} "
                    "— a phantom settle the gated image must never "
                    "mint"
                )
        if held_image != baseline:
            failures.append(
                f"quiesced-settle-failed: {label}'s pending write "
                f"landed on the quiesced standby's gated image — "
                f"serves {held_image} where the baseline {baseline} "
                "was owed"
            )
        if not held["pair"]:
            failures.append(
                "the deployed pair left its settled tracking layout "
                f"while the standby held the carried command — duty "
                f"{held_duty.get('role')!r}, standby {held_standby}"
            )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "hold",
                "gated": "held",
                "scans": ADOPT_SCANS + HOLD_SCANS,
                "image": held_image,
                "standby": held_standby.get("sync"),
            }
        )

        # Phase 5 — the true boundary and the owner's lead: the field
        # owner's first scan applies the admission — its journaled
        # settle is the line record every adopted resolution must
        # carry verbatim. The quiesced standby's run clock stands
        # ahead of it — its scans ticked while the owner's checkpoint
        # froze — so the owner keeps scanning until its tick passes
        # the standby's: the promotion's final-sync fetch must land a
        # checkpoint at or ahead of the standby's clock, the fresh
        # adoption that carries the settled record verbatim, never
        # the stale-clock carry that would leave the covered
        # admission to settle on the promoted peer's own boundary.
        # Under `unresolved-boundary` the leg withholds the apply
        # scan entirely — the audit must report the carried command
        # never resolved.
        standby_tick = held_standby.get("tick")
        if not isinstance(standby_tick, int):
            failures.append(
                f"the quiesced standby's role report carries no run "
                f"tick — {held_standby}"
            )
            raise Abort
        if tamper != "unresolved-boundary":
            owner_tick = pair.scan(duty_url, failures)["tick"]
            for _ in range(2 * (ADOPT_SCANS + HOLD_SCANS + RESOLVE_SCANS)):
                if owner_tick > standby_tick:
                    break
                owner_tick = pair.scan(duty_url, failures)["tick"]
            if owner_tick <= standby_tick:
                failures.append(
                    "the field owner's clock never passed the "
                    f"quiesced standby's — owner tick {owner_tick}, "
                    f"standby tick {standby_tick} — the promotion's "
                    "final-sync fetch would land stale"
                )
                raise Abort
        line_settles = admission_settles(
            pair.get(f"{duty_url}/journal", "GET /journal", failures),
            command,
        )
        if len(line_settles) != 1:
            diagnostic = (
                "quiesced-settle-failed"
                if not line_settles
                else "quiesced-settle-nondeterministic"
            )
            detail = (
                "the carried command has no true boundary to "
                "resolve at"
                if not line_settles
                else "the boundary resolves the carried command "
                "exactly once there"
            )
            failures.append(
                f"{diagnostic}: {label} journaled "
                f"{len(line_settles)} command_settled records on the "
                f"field owner — {detail}"
            )
            raise Abort
        line = line_settles[0][2]
        line_tick = applied_tick(line)
        if simulate.receipt_outcome(line) != "applied":
            failures.append(
                f"quiesced-settle-failed: the field owner settled the "
                f"carried admission {simulate.receipt_outcome(line)} "
                "— the staging expected the line's apply"
            )
            raise Abort
        evidence["line"] = {"settle": line_settles[0][:2], "tick": line_tick}
        digest_entries.append(
            {
                "phase": "boundary",
                "line": settle_rows(line_settles),
                "applied_tick": line_tick,
            }
        )

        # Phase 6 — the promotion: the quiesced standby's
        # promotion-boundary final sync carries the line's settled
        # record across — the carried command resolving exactly once
        # at its true boundary, the adopted record, never a fresh
        # local mint.
        promote = rig.promote(
            standby_url, failures, "the quiesced standby"
        )
        promoted = None
        watch = []
        for _ in range(SETTLE_SCANS):
            pair.scan(standby_url, failures)
            time.sleep(FETCH_SETTLE_S)
            report = pair.get(
                f"{standby_url}/role", "GET /role", failures
            )
            watch.append(report.get("role"))
            if report.get("role") == "active":
                promoted = report
                break
        evidence["promote"] = {"report": promote, "watch": watch}
        if promoted is None:
            failures.append(
                "the promoted standby never settled into the active "
                f"role — GET /role answers {report}"
            )
            raise Abort
        digest_entries.append(
            {"phase": "promote", "promote": promote, "watch": watch}
        )

        # Phase 7 — the boundary audit: exactly one `applied` settle
        # per peer at the line's own apply tick — the field owner's
        # boundary record plus the promoted peer's adopted one — one
        # terminal `applied` in each adopted log, the command's value
        # served on both images. A missing settle is the carried
        # command never resolved; a duplicate or divergent verdict
        # is the phantom contract's other face.
        journaled, logged, images = {}, {}, {}
        for name, url in (
            (duty_name, duty_url),
            (standby_name, standby_url),
        ):
            journaled[name] = admission_settles(
                pair.get(f"{url}/journal", "GET /journal", failures),
                command,
            )
            logged[name] = admission_receipts(
                pair.get(f"{url}/receipts", "GET /receipts", failures),
                command,
            )
            images[name] = simulate.snapshot_point(
                pair.get(f"{url}/snapshot", "GET /snapshot", failures),
                point,
            )
        for name in (duty_name, standby_name):
            entries = journaled[name]
            if len(entries) != 1:
                diagnostic = (
                    "quiesced-settle-failed"
                    if not entries
                    else "quiesced-settle-nondeterministic"
                )
                failures.append(
                    f"{diagnostic}: {label} journaled {len(entries)} "
                    f"command_settled records on {name} — the "
                    "boundary resolves the carried command exactly "
                    "once there"
                )
                continue
            receipt = entries[0][2]
            if simulate.receipt_outcome(receipt) != "applied":
                failures.append(
                    f"quiesced-settle-nondeterministic: {label} "
                    f"settled {simulate.receipt_outcome(receipt)} on "
                    f"{name} — the line applied it, so the carried "
                    "command's only resolution is applied"
                )
            elif line_tick is not None and applied_tick(receipt) != line_tick:
                failures.append(
                    f"quiesced-settle-nondeterministic: {label} "
                    f"settled applied on {name} at tick "
                    f"{applied_tick(receipt)} where the line's "
                    f"boundary applied it at {line_tick} — a fresh "
                    "mint on this peer, never the adopted record"
                )
        for name in (duty_name, standby_name):
            served = logged[name]
            if len(served) != 1 or simulate.receipt_outcome(served[0]) != "applied":
                failures.append(
                    f"quiesced-settle-failed: {name}'s adopted log "
                    f"carries "
                    f"{[simulate.receipt_outcome(receipt) for receipt in served]} "
                    f"for {label} — the terminal verdict owed is one "
                    "applied"
                )
            if images[name] != value:
                failures.append(
                    f"quiesced-settle-failed: {name}'s image never "
                    f"took {label}'s applied write — serves "
                    f"{images[name]}, expected {value}"
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
                "promotion — the adopted audit is not one log"
            )
        if failures:
            raise Abort
        evidence["audit"] = {
            "journaled": {
                name: settle_rows(entries)
                for name, entries in journaled.items()
            },
            "logged": {
                name: [simulate.receipt_outcome(receipt) for receipt in served]
                for name, served in logged.items()
            },
            "images": images,
        }
        digest_entries.append(
            {
                "phase": "audit",
                "journaled": evidence["audit"]["journaled"],
                "logged": evidence["audit"]["logged"],
                "images": images,
            }
        )

        # Phase 8 — the durable half: each peer's declared journal
        # file carries the same one settle the served monitor did —
        # the standby's file holding nothing ahead of the boundary
        # and exactly the adopted record after it.
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
                continue
            durable[name] = admission_settles(
                journal_entries(journal_path), command
            )
        for name in (duty_name, standby_name):
            if name not in durable:
                continue
            if settle_rows(durable[name]) != settle_rows(journaled[name]):
                failures.append(
                    f"quiesced-settle-failed: {name}'s durable "
                    f"journal carries {settle_rows(durable[name])} "
                    f"for {label} where its served journal carries "
                    f"{settle_rows(journaled[name])} — the durable "
                    "audit is not the served audit"
                )
        if failures:
            raise Abort
        evidence["durable"] = {
            name: settle_rows(entries) for name, entries in durable.items()
        }
        digest_entries.append(
            {"phase": "durable", "durable": evidence["durable"]}
        )

        # Phase 9 — the launch roles: the preempted field owner's
        # scans fence it demoting to a standby tracking the announced
        # successor the promoted peer's claim declared, the promoted
        # peer's documented demote releases the field, the
        # reconverged duty member's promote reclaims it, and the
        # standby settles `tracking` — the manifest's arrangement the
        # pair rests on.
        incumbent = None
        postures = []
        for _ in range(RESOLVE_SCANS):
            pair.scan(duty_url, failures)
            time.sleep(FETCH_SETTLE_S)
            report = pair.get(f"{duty_url}/role", "GET /role", failures)
            postures.append(claim_reclaim.sync_state(report))
            if claim_reclaim.tracking(report):
                incumbent = report
                break
        evidence["demote_watch"] = postures
        if incumbent is None:
            failures.append(
                "the preempted field owner never demoted to a "
                f"tracking standby — postures {postures}"
            )
            raise Abort
        restored = rig.switch(
            standby_url,
            duty_url,
            failures,
            demote_what="the promoted peer",
            promote_what="the reconverged duty member",
            promote_note=" restoring the launch roles",
            audit_receipts=True,
        )
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
        evidence["restored_at"] = restore_ticks[-1]
        digest_entries.append(
            {
                "phase": "restore",
                "demote": restored["demote"].get("role"),
                "promote": restored["promote"].get("role"),
                "demoted_role": restored["demoted_role"].get("role"),
                "promoted_role": restored["promoted_role"].get("role"),
                "transitions": {
                    name: [
                        {"from": frm, "to": to}
                        for _tick, frm, to in entries
                    ]
                    for name, entries in restored["transitions"].items()
                },
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
        choices=["planted-phantom", "unresolved-boundary"],
        help="doctor the run — a journaled settle planted into the "
        "quiesced standby's pending-window audit, or the owner's "
        "apply scan withheld while the leg asserts the boundary's "
        "resolution; each must fail naming the diagnostic",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = quiesced_settle_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                f"quiesced-standby-settle: the {args.tamper} case "
                "wanted the run to surface its named diagnostic — an "
                "inconclusive run offers the doctored case no "
                "evidence"
            )
            return 1
        eprint(
            f"quiesced-standby-settle: inconclusive — {inconclusive}"
        )
        print(
            f"quiesced-standby-settle-digest inconclusive — "
            f"{inconclusive}"
        )
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"quiesced-standby-settle: {line}")
        return 1
    for failure in failures:
        eprint(f"quiesced-standby-settle: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"quiesced-standby-settle: the {args.tamper} case "
                "passed silently — the leg never noticed the "
                "doctored evidence"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"quiesced-standby-settle-digest {digest} — the carried "
        f"admission held pending across "
        f"{ADOPT_SCANS + HOLD_SCANS} quiesced scans, resolved "
        f"applied once per peer at the line's own boundary, the "
        "launch roles restored"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
