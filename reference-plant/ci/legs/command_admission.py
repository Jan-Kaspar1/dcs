#!/usr/bin/env python3
"""The bounded command-admission leg for the reference plant — the
consumer-side proof that the bounded command-ingress contract holds
on the customer-owned redundant pair (WW-ENG-003, WW-FND-004): a
receipted-command flood past the served queue bound answers the named
`queue_full` admission verdict rather than queueing, settling, or
delaying a scan, and the admission metrics ride the snapshot's
`command_queue` section.

The platform-side suites pin the contract on the executor and the
monitor (the saturated-queue matrix, the admission-flood regression),
and the QA lane's command-admission scenario exercises it on the
simulated rig; this leg pins it on the pair the customer deployment
actually runs — `pair.launch_pair` spawning the manifest-declared
controllers on the released tooling. The run:

- converges the declared standby to `tracking` through the pair leg's
  driven-tick loop, then reads the field owner's served
  `command_queue.capacity` — the declared bound the flood must cross;
- submits twice the bound of receipted `write_value` commands against
  the owner's `POST /command`, rotating across the model's writable
  boolean points — a driven run drains the pending queue only at a
  scan boundary, so the back-to-back submissions outpace the drain —
  and asserts every submission answers 200 with a structured receipt:
  the first `capacity` `accepted`, each submission past the bound the
  named `queue_full` rejection carrying the bound and the refused
  target — terminal at admission, never queued;
- holds the queue at its bound while the served surface stays up —
  `/snapshot`, `/role`, and `/receipts` answering on both peers, the
  run's tick unmoved, the roles standing — and the live receipt
  mirror already carries the flood's verdicts in submission order,
  the overflow terminal `rejected` rather than pending;
- drives the drain: the first `POST /scan` after the flood lands the
  owner's tick exactly one boundary on — no queued submission held
  the scan — then tracking-first pair ticks carry the drained image
  and the settled receipt log onto the standby;
- audits the record: both peers' snapshots report the same
  `command_queue` section — every submission an `attempt`, the
  over-bound half `full_rejections`, `high_water` at the bound,
  `depth` drained to zero — the adopted receipt logs answer
  identically with the admitted half `applied` in submission order
  and the overflow still `rejected` `queue_full`, never settled, and
  the field owner's served journal carries each overflow's rejection
  at the flood's tick and each admitted command's `command_settled`
  in apply order at the drain tick;
- leaves the pair in its launch roles: the field owner `active`, the
  standby `tracking`.

Usage:

    command_admission.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `command-admission-digest <sha256>` line prints — the
check runs two passes and compares them
(`command-admission-nondeterministic`). A contract violation reports
`command-admission: …` lines on stderr and exits 1 — the check's
`command-admission-failed`. The doctored cases prove the leg's
assertions fire: `--tamper standby-flood` points the flood at the
tracking peer's role boundary, whose `not_active` refusals never
answer the named `queue_full` verdict, and `--tamper delayed-scan`
spends one extra owner scan before the cadence check so the measured
drain boundary lands a tick late.
"""

import argparse
import hashlib
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import pair
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored cases: a flood that receives no `queue_full` verdict
# — the tracking peer's role boundary answers `not_active` instead —
# and a drain landing past its boundary must each surface the named
# diagnostic — never a silently unexercised contract.
LEG = {
    "order": 260,
    "title": "the command-admission leg",
    "passes": "command-admission",
    "tampers": [
        {
            "name": "standby-flood",
            "passed": "a standby-flood case passed the admission leg",
            "missed": "the standby-flood case did not report its named diagnostic",
            "evidence": ["never answered the named queue_full"],
        },
        {
            "name": "delayed-scan",
            "passed": "a delayed-scan case passed the admission leg",
            "missed": "the delayed-scan case did not report its named diagnostic",
            "evidence": ["delayed the scan boundary"],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

ACTOR = "ci-command-admission"

# The tracking-first pair ticks the drain phase drives — comfortably
# past the adopted settlement's one-pull lag — and the served bound
# past which the leg's flood volume cannot reach.
DRAIN_TICKS = 3
CAPACITY_LIMIT = 512


def writable_bool_points(model):
    """The writable boolean `in` point ids the emitted model declares,
    in point order — the receipted `write_value` targets the flood
    rotates across. `requires_reason`-marked points are excluded: the
    flood submits reasonless, and a marked point's admission gate is
    the shelving-reason leg's contract, not this one's."""
    return [
        point["id"]
        for point in sorted(model["io_points"], key=lambda entry: entry["id"])
        if point.get("writable")
        and point["direction"] == "in"
        and point["value_type"] == "bool"
        and not point.get("requires_reason")
    ]


def flood_command(points, index):
    """The flood's `index`th command — rotating across the declared
    writable points with alternating values so the receipt log's
    submission order is observable positionally."""
    return {
        "write_value": {
            "point": points[index % len(points)],
            "kind": "bool",
            "value": {"bool": index % 2 == 0},
        }
    }


def tracking(role):
    """Whether a `GET /role` report is the settled tracking standby."""
    sync = role.get("sync") if isinstance(role, dict) else None
    return (
        role.get("role") == "standby"
        and isinstance(sync, dict)
        and "tracking" in sync
    )


def flood_receipts(receipts):
    """The leg's submissions out of a served receipt log — filtered on
    the leg's actor, in the log's submission order."""
    return [entry for entry in receipts if entry.get("actor") == ACTOR]


def flood_settlements(journal):
    """The leg's `command_settled` entries out of a served journal, in
    `seq` order — `(tick, command, outcome)` per settlement."""
    found = []
    for entry in journal:
        settled = entry.get("event", {}).get("command_settled")
        if settled is None:
            continue
        receipt = settled.get("receipt", {})
        if receipt.get("actor") != ACTOR:
            continue
        found.append(
            (
                entry["seq"],
                entry["tick"],
                receipt.get("command"),
                simulate.receipt_outcome(receipt),
            )
        )
    return found


def admission_pass(args, tamper):
    """The admission run: converge, flood, hold, drain, audit.
    Returns `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "command-admission leg has nothing to exercise"
        )
    with open(args.model) as handle:
        model = json.load(handle)
    points = writable_bool_points(model)
    if not points:
        raise Abort(
            "the emitted model declares no writable boolean point — "
            "the command-admission leg has nothing to flood"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url

        # Phase 1 — convergence, then the declared bound the flood
        # must cross off the field owner's served telemetry.
        converged = rig.converge(failures)
        owner = converged["owner"]
        pre_tick = owner["tick"]
        queue = owner.get("command_queue") or {}
        capacity = queue.get("capacity")
        if (
            not isinstance(capacity, int)
            or isinstance(capacity, bool)
            or capacity < 1
        ):
            raise Abort(
                "the field owner's snapshot serves no usable "
                f"command_queue capacity — the section reads {queue}"
            )
        if capacity > CAPACITY_LIMIT:
            raise Abort(
                f"the served command_queue capacity {capacity} is "
                f"beyond the leg's flood reach ({CAPACITY_LIMIT})"
            )
        baseline_attempts = queue.get("attempts", 0)
        baseline_rejections = queue.get("full_rejections", 0)
        evidence["converged"] = pre_tick
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
                "capacity": capacity,
            }
        )

        # Phase 2 — the flood: twice the declared bound of receipted
        # writable-point writes back-to-back against the owner while
        # nothing scans to drain the queue. The `standby-flood` tamper
        # points the same flood at the tracking peer's role boundary,
        # which answers `not_active` and never `queue_full`.
        flood_count = 2 * capacity
        target = standby_url if tamper == "standby-flood" else duty_url
        submissions = [
            flood_command(points, index) for index in range(flood_count)
        ]
        replies = [
            pair.request(
                f"{target}/command",
                {"command": command, "actor": ACTOR},
            )
            for command in submissions
        ]
        outcomes = []
        for index, (status, receipt) in enumerate(replies):
            if (
                status != 200
                or not isinstance(receipt, dict)
                or not isinstance(receipt.get("outcome"), dict)
            ):
                failures.append(
                    f"the flood's submission {index} answered "
                    f"{status} {receipt} — the bounded ingress owes "
                    "every submission a structured receipt"
                )
                continue
            outcomes.append(simulate.receipt_outcome(receipt))
            if (
                receipt.get("actor") != ACTOR
                or receipt.get("command") != submissions[index]
            ):
                failures.append(
                    f"the flood's submission {index} echoed receipt "
                    f"{receipt} — the answer is not the submission's "
                    "own receipt"
                )
        if failures:
            raise Abort
        early = [
            (index, outcome)
            for index, outcome in enumerate(outcomes[:capacity])
            if outcome != "accepted"
        ]
        if early:
            failures.append(
                f"submissions inside the declared bound answered "
                f"{early[:4]}, expected accepted — the queue refused "
                "before its bound"
            )
        missed = [
            (capacity + index, outcome)
            for index, outcome in enumerate(outcomes[capacity:])
            if outcome != "queue_full"
        ]
        if missed:
            failures.append(
                "the flood past the declared queue bound never "
                "answered the named queue_full verdict — submission "
                f"{missed[0][0]} answered {missed[0][1]!r} instead"
            )
        if failures:
            raise Abort
        for index in range(capacity, flood_count):
            reason = (
                replies[index][1]
                .get("outcome", {})
                .get("rejected", {})
                .get("reason", {})
            )
            verdict = reason.get("queue_full") or {}
            if (
                verdict.get("capacity") != capacity
                or verdict.get("point")
                != submissions[index]["write_value"]["point"]
            ):
                failures.append(
                    f"submission {index}'s rejection reads {reason} — "
                    "the queue_full verdict must name the declared "
                    "bound and the refused target"
                )
                break
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "flood",
                "capacity": capacity,
                "submissions": flood_count,
                "outcomes": outcomes,
                "first_refusal": replies[capacity][1],
            }
        )

        # Phase 3 — the held queue: the flood sits at the bound while
        # the served surface keeps answering — the reads, the run's
        # tick, and the roles must all stand unmoved — and the live
        # receipt mirror already carries every verdict in order, the
        # overflow terminal `rejected` rather than queued.
        held = pair.get(f"{duty_url}/snapshot", "GET /snapshot", failures)
        held_duty = pair.get(f"{duty_url}/role", "GET /role", failures)
        held_standby = pair.get(
            f"{standby_url}/role", "GET /role", failures
        )
        pair.get(f"{standby_url}/snapshot", "GET /snapshot", failures)
        held_receipts = pair.get(
            f"{duty_url}/receipts", "GET /receipts", failures
        )
        if held.get("tick") != pre_tick:
            failures.append(
                f"the owner's served tick moved to "
                f"{held.get('tick')} with no scan driven — the flood "
                "reached the run's pacing"
            )
        if held_duty.get("role") != "active":
            failures.append(
                f"the field owner reports {held_duty.get('role')!r} "
                "under the held flood, expected active"
            )
        if not tracking(held_standby):
            failures.append(
                f"the tracking peer reports {held_standby} under the "
                "held flood, expected standby/tracking"
            )
        mine = flood_receipts(held_receipts)
        if len(mine) != flood_count:
            failures.append(
                f"the live receipt mirror holds {len(mine)} flood "
                f"receipts, expected {flood_count} — a submission "
                "dropped without a receipt"
            )
        else:
            for index, receipt in enumerate(mine):
                want = "accepted" if index < capacity else "queue_full"
                if (
                    receipt.get("command") != submissions[index]
                    or simulate.receipt_outcome(receipt) != want
                ):
                    failures.append(
                        f"the receipt mirror's flood entry {index} "
                        f"reads {receipt}, expected submission "
                        f"{index}'s {want} — the log fell out of "
                        "submission order or an overflow queued"
                    )
                    break
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "held",
                "receipts": len(held_receipts),
                "duty_role": held_duty,
                "standby_role": held_standby,
            }
        )

        # Phase 4 — the drain: the first driven scan after the flood
        # lands the owner's tick exactly one boundary on — a queued
        # flood delayed no scan — then tracking-first pair ticks adopt
        # the drained, settled line onto the standby. The
        # `delayed-scan` tamper spends the drain before the measured
        # boundary so the cadence assertion must fire.
        if tamper == "delayed-scan":
            pair.scan(duty_url, failures)
        drained = pair.scan(duty_url, failures)
        if drained.get("tick") != pre_tick + 1:
            failures.append(
                f"the flood's drain scan landed at tick "
                f"{drained.get('tick')}, expected {pre_tick + 1} — a "
                "queued submission delayed the scan boundary"
            )
            raise Abort
        evidence["drained"] = drained["tick"]
        owner = drained
        drain_ticks = [drained["tick"]]
        for _ in range(DRAIN_TICKS):
            _tracked, owner = rig.tick(standby_url, duty_url, failures)
            drain_ticks.append(owner["tick"])
        digest_entries.append({"phase": "drain", "ticks": drain_ticks})

        # Phase 5 — the audit: the admission metrics ride the served
        # `command_queue` section identically on both peers — the
        # counters adopt with the checkpoint — the receipt logs are
        # one adopted log settling the admitted half `applied` in
        # submission order while the overflow stays `queue_full`,
        # and the owner's served journal records the overflow's
        # rejections at the flood's tick and the admitted commands'
        # settlements in apply order at the drain.
        duty_queue = (
            pair.get(f"{duty_url}/snapshot", "GET /snapshot", failures)
            .get("command_queue")
            or {}
        )
        standby_queue = (
            pair.get(
                f"{standby_url}/snapshot", "GET /snapshot", failures
            ).get("command_queue")
            or {}
        )
        wanted = {
            "attempts": baseline_attempts + flood_count,
            "full_rejections": baseline_rejections + capacity,
            "capacity": capacity,
            "depth": 0,
            "high_water": capacity,
        }
        for field, want in wanted.items():
            if duty_queue.get(field) != want:
                failures.append(
                    f"the owner's command_queue.{field} reports "
                    f"{duty_queue.get(field)}, expected {want} — the "
                    "flood's admission metrics did not ride the "
                    "snapshot"
                )
        if standby_queue != duty_queue:
            failures.append(
                f"the tracking peer's command_queue section reads "
                f"{standby_queue} against the field owner's "
                f"{duty_queue} — the adopted admission audit is not "
                "one record"
            )
        receipts_duty = pair.get(
            f"{duty_url}/receipts", "GET /receipts", failures
        )
        receipts_standby = pair.get(
            f"{standby_url}/receipts", "GET /receipts", failures
        )
        if receipts_duty != receipts_standby:
            failures.append(
                "the peers' receipt logs diverged across the drain "
                "— the adopted audit is not one log"
            )
        mine = flood_receipts(receipts_duty)
        if len(mine) != flood_count:
            failures.append(
                f"the settled receipt log holds {len(mine)} flood "
                f"receipts, expected {flood_count}"
            )
        else:
            for index, receipt in enumerate(mine):
                want = "applied" if index < capacity else "queue_full"
                if (
                    receipt.get("command") != submissions[index]
                    or simulate.receipt_outcome(receipt) != want
                ):
                    failures.append(
                        f"the settled log's flood entry {index} reads "
                        f"{receipt} — the admitted commands did not "
                        "settle in submission order, or an overflow "
                        "settled"
                    )
                    break
        journal = pair.get(f"{duty_url}/journal", "GET /journal", failures)
        settled = flood_settlements(journal)
        applied = [entry for entry in settled if entry[3] == "applied"]
        refused = [entry for entry in settled if entry[3] == "queue_full"]
        if [entry[2] for entry in applied] != submissions[:capacity]:
            failures.append(
                "the journaled settlements are not the admitted "
                "commands in submission order"
            )
        if [entry[2] for entry in refused] != submissions[capacity:]:
            failures.append(
                "the journaled queue_full rejections are not the "
                "overflow submissions in order"
            )
        if any(entry[1] != pre_tick for entry in refused):
            failures.append(
                "an overflow rejection journaled off the flood's tick "
                "— the refusal was not terminal at admission"
            )
        if any(entry[1] != drained["tick"] for entry in applied):
            failures.append(
                "an admitted command settled off the drain tick — the "
                "flood's queue drained late"
            )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "audit",
                "duty_queue": duty_queue,
                "standby_queue": standby_queue,
                "settled": {
                    "applied": len(applied),
                    "queue_full": len(refused),
                },
                "receipts": len(receipts_duty),
            }
        )

        # Phase 6 — the pair stands in its launch roles: the field
        # owner active, the standby tracking.
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        standby_role = pair.get(
            f"{standby_url}/role", "GET /role", failures
        )
        if duty_role.get("role") != "active" or not tracking(
            standby_role
        ):
            failures.append(
                f"the pair's launch roles moved under the flood — "
                f"the field owner reports {duty_role}, the tracking "
                f"peer {standby_role}"
            )
            raise Abort
        evidence["final_tick"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "roles",
                "duty_role": duty_role,
                "standby_role": standby_role,
            }
        )
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
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
        choices=["standby-flood", "delayed-scan"],
        help="doctor the leg — the pass must fail naming the evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = admission_pass(
            args, args.tamper
        )
    except Abort as abort:
        for line in abort.args:
            eprint(f"command-admission: {line}")
        return 1
    for failure in failures:
        eprint(f"command-admission: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"command-admission: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored run"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"command-admission-digest {digest} — {evidence['drained'] - evidence['converged']} "
        f"boundary landed the {2 * digest_entries[1]['capacity']}-command "
        f"flood: {digest_entries[1]['capacity']} admitted and settled in "
        f"order, the overflow queue_full at the declared bound "
        f"{digest_entries[1]['capacity']}, both peers' command_queue "
        "sections identical, roles unchanged at tick "
        f"{evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
