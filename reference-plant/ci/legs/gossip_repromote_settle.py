#!/usr/bin/env python3
"""The gossip-repromote-settle leg for the reference plant — the
consumer-side proof that a receipted command a holder suspends
`Accepted` at its own demote resolves when that same holder is
re-promoted inside the gossip window — before any peer's tracking
pull has covered the admission (WW-ENG-003, WW-LCM-001 — the
receipt-as-truth clause the #708 finding named: a pending command
must never sit `Accepted` against a depth-0 queue on the
field-owning run, and a still-served receipt must never let a
successor resurrect the stale write after newer commands have
settled).

The repromote-suspended-settle leg
(`ci/legs/repromote_suspended_settle.py`) proves the post-
reconvergence re-promote: it stages the suspension on a *fenced*
demote forced by a sibling's preempting promote, then lets the
demoted holder reconverge on the promoted sibling's checkpoint stream
before the re-promote. This leg is the variant that leg explicitly
scopes away — the window that window cannot reach. The suspension is
staged on the holder's *own* requested demote, inside one scan window
of the admission, and the re-promote lands the moment the demoted
holder reaches a promotable verdict — before the sibling's next
tracking pull can carry the admission back covered. In that window
the only adjudication that can run is the re-promoted run's own
field-owning boundary, which is the whole contract under test.

The driven pair makes the ordering deterministic without the rig's
free-running polls: a peer's pulls and applies live only inside its
own `POST /scan`, so which peer the leg scans is the staging
instrument. This leg scans only the demoted holder between the
demote and the re-promote — the sibling never runs, so its served
window provably never covered the admission, the leg asserts as much
before promoting. The run:

- converges the declared pair to `tracking` through the pair leg's
  driven-tick loop and gates the contract surface — both peers'
  served checkpoints carrying the receipt window and admission
  counters the index correlation reads, plus the manifest's declared
  journal files the durable audit half needs;
- submits a receipted `write_value` on a declared writable internal
  `In` point on the field owner and demotes that same owner inside
  the same scan window — the demote boundary suspends the pending
  receipt back to `Accepted` at its recorded index, the point never
  written and the command queue empty;
- drives only the demoted holder's scans until it reports a
  promotable verdict on the sibling's checkpoint stream (`orphaned`
  under the ownerless line, `tracking` once the sibling owns), then
  asserts the gossip window stood open: the sibling's submission
  high-water still mints at the admission's own index, its served log
  holds no receipt for it, and the holder's copy still stands
  `accepted` — no covering adoption ever adjudicated the tail;
- re-promotes that same holder inside the window — its first
  field-owning scan re-queues the suspended tail and settles it —
  and drives the demoted sibling's reconvergence, whose tracking
  pulls adopt the settled line;
- audits both peers' serving monitors and both declared durable
  journals: the point serves the command's value, `/receipts`
  carries the terminal `applied` verdict rather than a parked
  `accepted`, and exactly one `command_settled` per admission stands
  on each peer's journal;
- submits a newer write on the same point and asserts the ordering
  the finding's zombie half names: the newer command settles at a
  later tick than the re-promoted boundary's own settle, so the
  older suspended write never applies after it;
- switches the field to the sibling — the shape that resurrected the
  stale write while the receipt was parked — and asserts the
  successor applies nothing: one settlement per admission per peer
  still stands and the promoted peer serves the newer value, not the
  stale one;
- restores the pair's launch roles — the manifest-declared duty
  controller `active`, its standby `tracking`.

The contract postdates the release line's v0.3.0 cut: where the
launched tooling predates it the run's own evidence is the
pre-contract shape — a checkpoint carrying no receipt window or
admission counters, a receipt dropping its declared
`actor`/`reason`, a demote settling or dropping the admission rather
than suspending it, a served window that already covered or
adjudicated the admission, or a demoted holder that never reaches a
promotable verdict — and the leg reports
`gossip-repromote-settle-digest inconclusive` rather than asserting
until the manifest repins a release carrying the contract.

Usage:

    gossip_repromote_settle.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `gossip-repromote-settle-digest <sha256>` line
prints — the check runs two passes and compares them
(`gossip-repromote-settle-nondeterministic`). A contract violation
reports `gossip-repromote-settle: …` lines on stderr and exits 1 —
the check's `gossip-repromote-settle-failed`. `--tamper
expect-parked` doctors the leg's own expectation to the defect shape
— the suspended admission staying parked `Accepted` past the
re-promoted boundary — and `--tamper expect-stale` to the zombie
half, the promoted successor serving the older value; both must fail
on the honest record, proving the leg's audits fire rather than
passing unexercised.
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
import repromote_suspended_settle as sibling
import simulate
import takeover


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases. The
# doctored cases: a leg asserting the suspended admission may stay
# parked `Accepted` past the re-promoted holder's boundary, and a
# leg asserting the promoted successor serves the older value, must
# each surface the named diagnostic on the honest record rather than
# passing unexercised contracts.
LEG = {
    "order": 495,
    "title": "the gossip-repromote-settle leg",
    "passes": "gossip-repromote-settle",
    "tampers": [
        {
            "name": "expect-parked",
            "passed": "an expect-parked case passed the gossip-repromote-settle leg",
            "missed": "the expect-parked case did not report its named diagnostic",
            "evidence": ["the doctored expectation wanted the suspended admission parked"],
        },
        {
            "name": "expect-stale",
            "passed": "an expect-stale case passed the gossip-repromote-settle leg",
            "missed": "the expect-stale case did not report its named diagnostic",
            "evidence": ["the doctored expectation wanted the stale value"],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort
Inconclusive = sibling.Inconclusive

# The driven-scan bounds each transition gets: the re-promoted
# holder's settle into the field-owning role, the demoted holder's
# reconvergence to a promotable verdict inside the gossip window, the
# demoted sibling's adoption, the newer command's settle, and the
# launch-role hold train.
SETTLE_SCANS = 6
WINDOW_SCANS = 8
RESOLVE_SCANS = 10
NEWER_SCANS = 8
RESTORE_TICKS = 4

# The admissions' declared identities — unique in both peers' receipt
# logs, the (command, actor) pair each settle audit correlates by. The
# suspended admission and the newer command on the same point differ
# in value and in actor, so neither can be mistaken for the other.
ACTOR = "ci-gossip"
REASON = "gossip-repromote-settle"
NEWER_ACTOR = "ci-gossip-newer"
NEWER_REASON = "gossip-repromote-settle-newer"


def promotable(report):
    """Whether a served RoleReport carries a verdict `POST /promote`
    accepts — `standby` under the tracked-line sync states. The
    ownerless line the demotion leaves behind reports `orphaned`, the
    same promotable verdict `tracking` stands on."""
    sync = report.get("sync") if isinstance(report, dict) else None
    return (report or {}).get("role") == "standby" and (
        isinstance(sync, dict)
        and bool({"tracking", "orphaned", "reinitialized", "usurped"}
                 & set(sync))
    )


def gossip_pass(args, tamper):
    """The gossip-repromote-settle run: converge, suspend the admission
    on the holder's own demote inside one scan window, hold the gossip
    window open by scanning only the demoted holder, re-promote it
    inside the window, audit both monitors and both durable journals
    for the exactly-once settle, prove the ordering against a newer
    command on the same point across a switch to the successor, and
    restore the launch roles. Returns `(digest_entries, evidence,
    failures)`; raises `Inconclusive` where the pinned release
    predates the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "gossip-repromote-settle leg has nothing to exercise"
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
            "gossip-repromote-settle leg has nothing to suspend"
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

        # The contract surface: both peers' checkpoints must carry the
        # receipt window and admission counters the index correlation
        # reads, and the manifest must declare the journal files the
        # durable audit reads. A surface absent is the release
        # predating the contract, never a violation of it.
        for name, url in (
            (duty_name, duty_url),
            (standby_name, standby_url),
        ):
            sibling.receipt_window(url, f"GET /checkpoint on {name}", failures)
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
        stale = value
        fresh = {"bool": baseline["bool"]}
        command = takeover.write_value(point, value["bool"])
        newer = takeover.write_value(point, fresh["bool"])

        # The admission's index: the converged peers must agree on the
        # submission high-water — the coverage audit reads the
        # sibling's mint index against it.
        index = sibling.next_receipt_index(
            duty_url, "GET /checkpoint", failures
        )
        peer_index = sibling.next_receipt_index(
            standby_url, "GET /checkpoint", failures
        )
        evidence["indices"] = {"duty": index, "standby": peer_index}
        if index != peer_index:
            failures.append(
                "the converged peers disagree on the submission "
                f"high-water — the tracking peer mints at {peer_index} "
                f"where the owner mints at {index} — the converged "
                "line never stood"
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

        # Phase 2 — the suspension, inside one scan window: the
        # receipted write is admitted pending on the field owner and
        # the same owner's `POST /demote` lands before any scan of it
        # runs, so the demote boundary suspends the pending receipt
        # back to `Accepted` — the shape #708's reproduction
        # produced. Neither peer is scanned between the two calls: in
        # the driven pair a scan is the only thing that applies a
        # pending admission, so the demote is guaranteed the window.
        status, suspended = pair.request(
            f"{duty_url}/command",
            {
                "command": command,
                "actor": ACTOR,
                "reason": REASON,
            },
        )
        evidence["submission"] = {"status": status, "receipt": suspended}
        if status != 200 or simulate.receipt_outcome(suspended) != "accepted":
            failures.append(
                f"the admission answered {status} {suspended}, expected "
                f"an accepted receipt at index {index}"
            )
            raise Abort
        if (
            suspended.get("actor") != ACTOR
            or suspended.get("reason") != REASON
        ):
            raise Inconclusive(
                "the served receipt drops the declared actor/reason — "
                "the submission-record identity the settle audit "
                "correlates by; the pinned release predates the "
                "gossip-repromote-settle contract"
            )
        demote = rig.demote(duty_url, failures, "the field owner")
        standing = sibling.index_receipt(
            duty_url, index, "GET /checkpoint", failures
        )
        evidence["demote"] = demote
        evidence["suspended_standing"] = standing
        if not sibling.admission_hit(standing, command, ACTOR):
            raise Inconclusive(
                f"the admission's served receipt reads {standing} at "
                f"index {index} right after the demote — the "
                "admission never stood suspended; the pinned release "
                "predates the suspended-entry contract"
            )
        if simulate.receipt_outcome(standing) != "accepted":
            raise Inconclusive(
                "the demote settled the admission "
                f"{simulate.receipt_outcome(standing)} instead of "
                "suspending it — the pinned release predates the "
                "suspended-entry contract"
            )
        suspended_image = simulate.snapshot_point(
            pair.get(f"{duty_url}/snapshot", "GET /snapshot", failures),
            point,
        )
        evidence["suspended_image"] = suspended_image
        if suspended_image != baseline:
            failures.append(
                f"the demoted holder's image reads {suspended_image} "
                f"for point {point}, expected the baseline {baseline} "
                "— a suspended admission wrote through the demote"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "suspend",
                "index": index,
                "point": point,
                "demote": demote,
                "standing": "accepted",
                "image": "baseline",
            }
        )

        # Phase 3 — the gossip window, held open by construction: only
        # the demoted holder is scanned, and only until it reaches a
        # promotable verdict on the sibling's checkpoint stream. The
        # sibling's own pulls live inside its `POST /scan`, which never
        # runs here, so its served window cannot cover the admission —
        # which the next read proves rather than assumes.
        postures = []
        ready = None
        for _ in range(WINDOW_SCANS):
            pair.scan(duty_url, failures)
            report = pair.get(f"{duty_url}/role", "GET /role", failures)
            postures.append(sibling.sync_posture(report))
            if promotable(report):
                ready = report
                break
        evidence["window"] = {"postures": postures}
        if ready is None:
            failures.append(
                "the demoted holder never reached a promotable verdict "
                "on the sibling's checkpoint stream — postures "
                f"{postures}"
            )
            raise Abort

        # The window's proof: the sibling's submission high-water still
        # mints at the admission's index, its served log holds no
        # receipt for it, and the holder's copy still parks `accepted`
        # — no covering adoption ever adjudicated the tail. Any of the
        # three breaking means the tracked stream settled it first and
        # the re-promote contract is not what this run would exercise.
        peer_index = sibling.next_receipt_index(
            standby_url, "GET /checkpoint", failures
        )
        peer_logged = sibling.admission_receipts(
            standby_url, command, ACTOR, "GET /receipts", failures
        )
        holder_served = sibling.index_receipt(
            duty_url, index, "GET /checkpoint", failures
        )
        evidence["pre_repromote"] = {
            "postures": postures,
            "sibling_next_index": peer_index,
            "sibling_receipts": peer_logged,
            "holder_receipt": holder_served,
        }
        if peer_logged or peer_index > index:
            raise Inconclusive(
                "the sibling's window covered the staged admission — "
                f"next index {peer_index}, logged {peer_logged} — its "
                "pull carried the receipt back covered: the "
                "gossip-window re-promote contract never engaged"
            )
        if holder_served is None or not sibling.admission_hit(
            holder_served, command, ACTOR
        ):
            raise Inconclusive(
                "the staged admission vanished across the "
                "reconvergence — the demoted peer exposes no "
                "suspended-entry shape the contract owes; the pinned "
                "release predates it"
            )
        if simulate.receipt_outcome(holder_served) != "accepted":
            raise Inconclusive(
                "the staged admission settled "
                f"{simulate.receipt_outcome(holder_served)} across "
                "the reconvergence — a covering adoption adjudicated "
                "it before the re-promote"
            )
        digest_entries.append(
            {
                "phase": "window",
                "postures": postures,
                "window": "open",
                "verdict": sibling.sync_posture(ready),
            }
        )

        # Phase 4 — the re-promote inside the window: the holder takes
        # the field back and its first field-owning scan re-queues the
        # suspended tail, settling it once. Nothing has adjudicated
        # that index — no adoption covered it, no promotion carried it
        # — so this boundary is the whole contract.
        repromote = rig.promote(duty_url, failures, "the demoted holder")
        settle_roles = []
        repromoted = None
        for _ in range(SETTLE_SCANS):
            pair.scan(duty_url, failures)
            report = pair.get(f"{duty_url}/role", "GET /role", failures)
            settle_roles.append(report.get("role"))
            if report.get("role") == "active":
                repromoted = report
                break
        evidence["repromote"] = {
            "promote": repromote,
            "watch": settle_roles,
        }
        if repromoted is None:
            failures.append(
                "the re-promoted holder never settled into the active "
                f"role — GET /role answers {report}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "repromote",
                "promote": repromote,
                "watch": settle_roles,
            }
        )

        # Phase 5 — the sibling's demote and reconvergence: the
        # sibling's pulls adopt the settled line, the adopted
        # `command_settled` record the once-per-peer audit expects on
        # it. The re-promoted owner is scanned ahead each round so its
        # served document stays strictly ahead of the tracker's.
        postures = []
        tracked = None
        for _ in range(RESOLVE_SCANS):
            pair.scan(duty_url, failures)
            pair.scan(standby_url, failures)
            report = pair.get(f"{standby_url}/role", "GET /role", failures)
            postures.append(sibling.sync_posture(report))
            if sibling.tracking(report):
                tracked = report
                break
        evidence["sibling_reconverge"] = {"postures": postures}
        if tracked is None:
            failures.append(
                "the demoted sibling never reconverged tracking behind "
                f"the re-promoted holder — postures {postures}"
            )
            raise Abort
        digest_entries.append(
            {"phase": "sibling", "postures": postures}
        )

        # Phase 6 — the served audit: both monitors' journals and
        # receipt logs, the served images, and the adopted one-log
        # proof. The contract: exactly one `command_settled` per
        # admission on each peer — `applied`, the re-promoted holder's
        # own boundary plus the demoted sibling's adopted record — the
        # served log carrying the terminal verdict rather than a
        # parked `accepted`, and the point serving the command's
        # value. Under `expect-parked` the leg doctors its
        # expectation to the defect shape — the admission still
        # `accepted` past the boundary.
        journals = {
            name: pair.get(f"{url}/journal", "GET /journal", failures)
            for name, url in (
                (duty_name, duty_url),
                (standby_name, standby_url),
            )
        }
        settles = {
            name: sibling.admission_settles(journal, command, ACTOR)
            for name, journal in journals.items()
        }
        logged = {
            name: sibling.admission_receipts(
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
            # suspended admission stays parked `accepted` past the
            # re-promoted holder's boundary, the defect the contract
            # closed. The honest applied record must fail it, naming
            # the settlement the run stood on.
            failures.append(
                "the doctored expectation wanted the suspended "
                f"admission parked Accepted past the re-promoted "
                f"boundary — {ACTOR} journaled {settles[duty_name]} on "
                "its holder and logged "
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
                    f"command_settled records for the admission — the "
                    f"contract owes exactly one there: {expected}"
                )
            elif settles[name][0][2] != "applied":
                failures.append(
                    f"{name}'s settle for the admission is "
                    f"{settles[name][0][2]} — the re-promoted holder's "
                    "boundary owes applied"
                )
        if outcomes and outcomes != {"applied"}:
            failures.append(
                f"the admission journaled {sorted(outcomes)} across the "
                "pair — one admission, never more than one terminal "
                "outcome"
            )
        for name in (duty_name, standby_name):
            parked = [
                receipt
                for receipt in logged[name]
                if simulate.receipt_outcome(receipt) == "accepted"
            ]
            if parked:
                failures.append(
                    f"the admission is still parked Accepted on {name} "
                    "— the re-promoted holder's boundary never "
                    "re-queued it"
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
                    f"{[simulate.receipt_outcome(r) for r in logged[name]]}"
                    " for the admission — the terminal verdict owed is "
                    "one applied"
                )
            if images[name] != value:
                failures.append(
                    f"{name}'s image reads {images[name]} for point "
                    f"{point}, expected the command's value {value} — a "
                    "settlement without the command's application"
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
        settled_tick = settles[duty_name][0][1] if settles[duty_name] else None
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

        # Phase 7 — the newer command on the same point, then the
        # switch that used to resurrect the stale write: the finding's
        # zombie half. The newer admission settles at a tick strictly
        # after the re-promoted boundary's own settle — the older
        # suspended write can never land after it — and the successor
        # the switch promotes applies nothing, serving the newer value.
        newer_receipt = pair.request(
            f"{duty_url}/command",
            {
                "command": newer,
                "actor": NEWER_ACTOR,
                "reason": NEWER_REASON,
            },
        )
        newer_status, newer_admission = newer_receipt
        evidence["newer_submission"] = {
            "status": newer_status,
            "receipt": newer_admission,
        }
        if (
            newer_status != 200
            or simulate.receipt_outcome(newer_admission) != "accepted"
        ):
            failures.append(
                f"the newer write answered {newer_status} "
                f"{newer_admission}, expected an accepted receipt on the "
                "re-promoted holder"
            )
            raise Abort
        newer_settles = []
        newer_ticks = []
        for _ in range(NEWER_SCANS):
            _tracked, owner = rig.tick(standby_url, duty_url, failures)
            newer_ticks.append(owner["tick"])
            newer_settles = sibling.admission_settles(
                pair.get(f"{duty_url}/journal", "GET /journal", failures),
                newer,
                NEWER_ACTOR,
            )
            if newer_settles:
                break
        evidence["newer_settles"] = newer_settles
        evidence["newer_ticks"] = newer_ticks
        if len(newer_settles) != 1 or newer_settles[0][2] != "applied":
            failures.append(
                "the newer write carries "
                f"{[entry[2] for entry in newer_settles]} settlements on "
                "the re-promoted holder, expected exactly one applied"
            )
            raise Abort
        if settled_tick is not None \
                and newer_settles[0][1] <= settled_tick:
            failures.append(
                f"the newer write settled at tick {newer_settles[0][1]}, "
                f"not after the re-promoted boundary's settle at "
                f"{settled_tick} — the older suspended write applied "
                "after the newer command"
            )
            raise Abort
        newer_image = simulate.snapshot_point(
            pair.get(f"{duty_url}/snapshot", "GET /snapshot", failures),
            point,
        )
        evidence["newer_image"] = newer_image
        if newer_image != fresh:
            failures.append(
                f"the re-promoted holder's image reads {newer_image} for "
                f"point {point}, expected the newer value {fresh}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "newer",
                "settles": newer_settles,
                "image": newer_image,
            }
        )

        switched = rig.switch(
            duty_url, standby_url, failures, audit_receipts=True
        )
        evidence["switched_at"] = switched["demote"]["tick"]
        promoted_image = simulate.snapshot_point(
            pair.get(f"{standby_url}/snapshot", "GET /snapshot", failures),
            point,
        )
        promoted_settles = {
            name: sibling.admission_settles(
                pair.get(f"{url}/journal", "GET /journal", failures),
                command,
                ACTOR,
            )
            for name, url in (
                (duty_name, duty_url),
                (standby_name, standby_url),
            )
        }
        evidence["successor"] = {
            "image": promoted_image,
            "settles": promoted_settles,
        }
        if tamper == "expect-stale":
            # The doctored expectation — the leg asserts the promoted
            # successor serves the older value, the zombie the parked
            # receipt let a later promotion write. The honest newer
            # value must fail it.
            failures.append(
                "the doctored expectation wanted the stale value "
                f"{stale} served on the promoted successor, where the "
                f"run stood on the newer value {fresh} — "
                f"{ACTOR} settled once at tick {settles[duty_name][0][1]}"
                f" and {NEWER_ACTOR} after it"
            )
            raise Abort
        if promoted_image != fresh:
            failures.append(
                f"the promoted successor's image reads "
                f"{promoted_image} for point {point}, expected the newer "
                f"value {fresh} — the stale suspended write applied "
                "after the newer command"
            )
            raise Abort
        for name in (duty_name, standby_name):
            if len(promoted_settles[name]) != 1:
                failures.append(
                    f"{name} carries {len(promoted_settles[name])} "
                    "settlements for the re-promoted admission after the "
                    "switch — a receipt that resolved at the "
                    "re-promoted boundary is never applied again"
                )
            elif promoted_settles[name][0][2] != "applied":
                failures.append(
                    f"{name}'s record for the re-promoted admission reads "
                    f"{promoted_settles[name][0][2]} after the switch — "
                    "the one settlement owed is applied"
                )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "successor",
                "image": promoted_image,
                "settles": promoted_settles,
                "switch": switched["demote"]["tick"],
            }
        )

        # Phase 8 — the durable half: each peer's declared journal
        # file carries the same one settle per admission its serving
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
                    f"{name}'s declared journal file {journal_path} does "
                    "not exist — the --journal-file flag was not honored"
                )
                raise Abort
            durable[name] = sibling.admission_settles(
                sibling.journal_entries(journal_path), command, ACTOR
            )
        for name in (duty_name, standby_name):
            if durable[name] != settles[name]:
                failures.append(
                    f"{name}'s durable journal carries {durable[name]} for "
                    f"the admission where its served journal carries "
                    f"{settles[name]} — the durable audit is not the "
                    "served audit"
                )
        if failures:
            raise Abort
        digest_entries.append({"phase": "durable", "durable": durable})

        # Phase 9 — the launch roles: the pair is switched back, the
        # duty controller `active` and its standby `tracking` — and the
        # hold train asserts the layout across repeated driven pulls,
        # each tick proving identical images.
        rig.demote(standby_url, failures, "the promoted successor")
        rig.promote(duty_url, failures, "the reconverged peer")
        restore_ticks = []
        for _ in range(RESTORE_TICKS):
            _tracked, owner = rig.tick(
                standby_url,
                duty_url,
                failures,
                diverged="the restored pair's images diverged at tick "
                "{tick} — the launch-role layout did not hold",
            )
            restore_ticks.append(owner["tick"])
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        standby_role = pair.get(
            f"{standby_url}/role", "GET /role", failures
        )
        if duty_role.get("role") != "active":
            failures.append(
                f"the restored field owner reports "
                f"{duty_role.get('role')!r}, expected active — the pair "
                "was not left in its launch roles"
            )
        if not sibling.tracking(standby_role):
            failures.append(
                "the demoted sibling never settled tracking behind the "
                f"restored owner — GET /role answers {standby_role}"
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
        choices=["expect-parked", "expect-stale"],
        help="doctor the leg's expectation to a defect shape — the pass "
        "must fail naming the settlement the honest run stood on",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = gossip_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "gossip-repromote-settle: the doctored "
                f"{args.tamper} case wants a defect shape an "
                "inconclusive run never reached — it offers the case "
                "no evidence"
            )
            return 1
        eprint(f"gossip-repromote-settle: inconclusive — {inconclusive}")
        print(
            f"gossip-repromote-settle-digest inconclusive — "
            f"{inconclusive}"
        )
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"gossip-repromote-settle: {line}")
        return 1
    for failure in failures:
        eprint(f"gossip-repromote-settle: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"gossip-repromote-settle: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored "
                "expectation"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"gossip-repromote-settle-digest {digest} — tracking by tick "
        f"{evidence['converged']}, the admission suspended accepted at "
        f"index {evidence['indices']['duty']} by its own holder's demote, "
        "re-promoted inside the gossip window before any peer's pull "
        "covered it and settled applied once on each peer's journal — "
        "durable included — the newer write on the same point settling "
        "after it and the successor applying nothing, launch roles "
        f"restored at tick {evidence['restored_at']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())