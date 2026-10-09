#!/usr/bin/env python3
"""Prove that an abandoned command wait still settles on the reference pair.

Converge the customer-owned redundant pair, pause the field owner, and send
one writable-point command under a short client bound. Resume the owner
and drive the admitted command through its scan boundary. Both receipt
logs and served/durable journals must agree on exactly one settlement,
with the pair remaining in its launch roles. Two passes must produce the
same digest. The skip-settle tamper omits the settling scan and must fail
the existing boundary check.
"""

import argparse
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import pair
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored case.
# The doctored case skips the real settlement scan so the scan-boundary
# audit must identify the omitted operation.
LEG = {
    "order": 470,
    "title": "the command-abort-verdict leg",
    "passes": "command-abort-verdict",
    "tampers": [
        {
            "name": "skip-settle",
            "passed": "a skip-settle case passed the abort-verdict leg",
            "missed": "the skip-settle case did not report its named diagnostic",
            "evidence": ["the settle scan landed at tick"],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The harness cannot stage the paused, unanswered submission."""


ACTOR = "ci-command-abort-verdict"

# The tightened client bound expires while the owner is paused, after
# the request bytes have been sent.
ABORT_BOUND_S = 0.5

# The admission's landing window after the restore — the buffered
# request's queue is processed the moment the monitor runs again, so
# this is a generous bound on scheduling, not on the contract.
ADMISSION_WAIT_S = 10.0
ADMISSION_POLL_S = 0.1

# The tracking-first pair ticks the settle phase drives — comfortably
# past the adopted settlement's one-pull lag.
SETTLE_TICKS = 3

def writable_bool_points(model):
    """The writable boolean `in` point ids the emitted model declares,
    in point order — the receipted `write_value` target the leg's
    submission carries. `requires_reason`-marked points are excluded:
    the leg submits reasonless, and a marked point's admission gate is
    the shelving-reason leg's contract, not this one's."""
    return [
        point["id"]
        for point in sorted(model["io_points"], key=lambda entry: entry["id"])
        if point.get("writable")
        and point["direction"] == "in"
        and point["value_type"] == "bool"
        and not point.get("requires_reason")
    ]


def tracking(role):
    """Whether a `GET /role` report is the settled tracking standby."""
    sync = role.get("sync") if isinstance(role, dict) else None
    return (
        role.get("role") == "standby"
        and isinstance(sync, dict)
        and "tracking" in sync
    )


def bounded_post(url, envelope, bound):
    """Send a bounded command request and classify its transport result.

    An unanswered result ends the client wait without determining the
    command settlement. An answered receipt or pre-admission refusal
    means the paused transport exercise did not form.
    """
    request = urllib.request.Request(
        url,
        data=json.dumps(envelope).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=bound) as response:
            return "answered", json.load(response)
    except urllib.error.HTTPError as error:
        body = error.read().decode(errors="replace")
        return "refused", error.code, body
    except Exception as error:
        return "unanswered", error


def pause_peer(process):
    """The field owner's container pause — `SIGSTOP` on the spawned
    process, the `docker pause` reproduction's lever beside
    `pair.stop`'s container stop: the monitor's listeners stay open so
    the submission's bytes sit buffered in the backlog while no answer
    can return. A lever that cannot land classifies inconclusive."""
    if not hasattr(signal, "SIGSTOP"):
        raise Inconclusive(
            "the consumer harness admits no stop/pause lever — no "
            "SIGSTOP"
        )
    try:
        process.send_signal(signal.SIGSTOP)
    except Exception as error:
        raise Inconclusive(
            f"the field-owner pause lever never landed: {error}"
        )


def resume_peer(process):
    """The pause's restore — `SIGCONT` so the buffered submission is
    read and admitted, the `docker unpause` the reproduction runs."""
    try:
        process.send_signal(signal.SIGCONT)
    except Exception:
        pass


def admission_receipt(url, actor, command, wait=ADMISSION_WAIT_S):
    """The leg's admission out of the owner's live receipt mirror —
    the buffered request's `accepted` receipt, polled for across the
    restore's landing window. Returns the receipt, or None when the
    abandoned submission never reached admission."""
    deadline = time.monotonic() + wait
    while True:
        try:
            for entry in simulate.http(f"{url}/receipts"):
                if (
                    entry.get("actor") == actor
                    and entry.get("command") == command
                ):
                    return entry
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return None
        time.sleep(ADMISSION_POLL_S)


def settlements(entries, actor, command):
    """The `command_settled` records matching the leg's submission out
    of a journal entry list — the durable file's `entry` records or
    the served journal's entries alike — `(seq, tick, outcome)` per
    match in `seq` order."""
    found = []
    for entry in entries:
        settled = entry.get("event", {}).get("command_settled")
        if settled is None:
            continue
        receipt = settled.get("receipt", {})
        if (
            receipt.get("actor") == actor
            and receipt.get("command") == command
        ):
            found.append(
                (
                    entry["seq"],
                    entry["tick"],
                    simulate.receipt_outcome(receipt),
                )
            )
    return found


def abort_verdict_pass(args, tamper):
    """The abort-verdict run: converge, abort, restore, settle,
    audit. Returns `(digest_entries, evidence, failures)`; raises
    `Inconclusive` where the pinned release predates the contract or
    the harness cannot land the exercise."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "command-abort-verdict leg has nothing to exercise"
        )
    with open(args.model) as handle:
        model = json.load(handle)
    points = writable_bool_points(model)
    if not points:
        raise Abort(
            "the emitted model declares no writable boolean point — "
            "the command-abort-verdict leg has nothing to submit"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url

        # Phase 1 — convergence: the manifest-declared pair settled,
        # the field owner active and the standby tracking.
        converged = rig.converge(failures)
        pre_tick = converged["owner"]["tick"]
        evidence["converged"] = pre_tick
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )

        # Phase 2: pause the owner and submit under a short client bound.
        # The request stays buffered while the owner cannot answer.
        command = {
            "write_value": {
                "point": points[0],
                "kind": "bool",
                "value": {"bool": True},
            }
        }
        envelope = {"command": command, "actor": ACTOR}
        pause_peer(rig.duty)
        try:
            end = bounded_post(
                f"{duty_url}/command", envelope, ABORT_BOUND_S
            )
        finally:
            resume_peer(rig.duty)
        if end[0] == "answered":
            raise Inconclusive(
                "the post's answer landed inside the tightened bound "
                "— the pause lever never held the response, so no "
                "abort was exercised"
            )
        if end[0] == "refused":
            failures.append(
                f"the writable-point submission answered a refusal "
                f"HTTP {end[1]}: {end[2]} — a valid receipted "
                "command must admit, never refuse pre-admission"
            )
            raise Abort
        digest_entries.append({"phase": "abort", "end": end[0]})

        # Phase 3 — the admission: the buffered submission lands once
        # the monitor runs again — the live receipt mirror carries
        # the leg's `accepted` receipt, proof the abandoned post's
        # outcome was genuinely open. A submission that never reaches
        # admission exercises no settle path — inconclusive, not a
        # verdict failure.
        receipt = admission_receipt(duty_url, ACTOR, command)
        if receipt is None:
            raise Inconclusive(
                "the abandoned submission never reached admission — "
                "the buffered request did not land, so the leg's "
                "settle half has nothing to exercise"
            )
        if simulate.receipt_outcome(receipt) != "accepted":
            failures.append(
                f"the abandoned submission's live receipt reads "
                f"{receipt} — the admission must queue accepted, "
                "never a refused or pre-settled record"
            )
            raise Abort
        evidence["admitted"] = receipt.get("outcome", {}).get(
            "accepted", {}
        ).get("apply_tick")
        digest_entries.append({"phase": "admission", "receipt": receipt})

        # Phase 4 — the settle: the first driven scan after the
        # restore lands the owner's tick exactly one boundary on and
        # applies the queued admission — then tracking-first pair
        # ticks adopt the settled record onto the standby.
        drained = (
            pair.get(f"{duty_url}/snapshot", "GET /snapshot", failures)
            if tamper == "skip-settle"
            else pair.scan(duty_url, failures)
        )
        if drained.get("tick") != pre_tick + 1:
            failures.append(
                f"the settle scan landed at tick {drained.get('tick')}, "
                f"expected {pre_tick + 1} — the aborted submission's "
                "queue held the scan boundary"
            )
            raise Abort
        settle_tick = drained["tick"]
        evidence["settle_tick"] = settle_tick
        owner = drained
        settle_ticks = [settle_tick]
        for _ in range(SETTLE_TICKS):
            _tracked, owner = rig.tick(standby_url, duty_url, failures)
            settle_ticks.append(owner["tick"])
        digest_entries.append({"phase": "settle", "ticks": settle_ticks})

        # Phase 5 — the audit: the served /receipts on both peers are
        # one adopted log carrying the leg's receipt terminal
        # `applied`; the owner's served journal and each peer's
        # durable journal file carry exactly one `command_settled`
        # for the admission at the settle tick — the standby's the
        # checkpoint-adopted copy of the same record.
        receipts_duty = pair.get(
            f"{duty_url}/receipts", "GET /receipts", failures
        )
        receipts_standby = pair.get(
            f"{standby_url}/receipts", "GET /receipts", failures
        )
        if receipts_duty != receipts_standby:
            failures.append(
                "the peers' receipt logs diverged across the settle "
                "— the adopted audit is not one log"
            )
        mine = [
            entry
            for entry in receipts_duty
            if entry.get("actor") == ACTOR
            and entry.get("command") == command
        ]
        if len(mine) != 1:
            failures.append(
                f"the settled receipt log holds {len(mine)} receipts "
                "for the aborted submission — exactly one admission "
                "must stand"
            )
        elif simulate.receipt_outcome(mine[0]) != "applied":
            failures.append(
                f"the aborted submission's settled receipt reads "
                f"{mine[0]} — the true terminal verdict is applied"
            )
        served_journal = pair.get(
            f"{duty_url}/journal", "GET /journal", failures
        )
        served_settles = settlements(served_journal, ACTOR, command)
        if len(served_settles) != 1:
            failures.append(
                f"the owner's served journal carries "
                f"{len(served_settles)} command_settled entries for "
                "the aborted submission — exactly one must stand for "
                "the admission"
            )
        elif served_settles[0][1:] != (settle_tick, "applied"):
            failures.append(
                f"the journaled settle reads {served_settles[0]} — "
                f"expected applied at the settle tick {settle_tick}"
            )
        for name, files in (
            ("field owner's", rig.duty_files),
            ("tracking peer's", rig.standby_files),
        ):
            durable_entries = [
                payload
                for kind, payload in pair.journal_records(
                    files["journal_file"]
                )
                if kind == "entry"
            ]
            durable_settles = settlements(
                durable_entries, ACTOR, command
            )
            if durable_settles != served_settles:
                failures.append(
                    f"the {name} durable journal carries "
                    f"{durable_settles} for the admission against "
                    f"the served {served_settles} — the file and the "
                    "served stream disagree on the terminal verdict"
                )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "audit",
                "receipt": mine[0],
                "settled": served_settles,
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
                f"the pair's launch roles moved under the aborted "
                f"submission — the field owner reports {duty_role}, "
                f"the tracking peer {standby_role}"
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
        choices=["skip-settle"],
        help="omit the settlement scan so the boundary check must fail",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = abort_verdict_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "command-abort-verdict: the skip-settle case has no "
                "settlement evidence in an inconclusive run"
            )
            return 1
        eprint(
            f"command-abort-verdict: inconclusive — {inconclusive}"
        )
        print(
            f"command-abort-verdict-digest inconclusive — {inconclusive}"
        )
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"command-abort-verdict: {line}")
        return 1
    for failure in failures:
        eprint(f"command-abort-verdict: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"command-abort-verdict: the {args.tamper} case "
                "passed silently — the leg never noticed the doctored "
                "settlement"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"command-abort-verdict-digest {digest}: the abandoned bounded "
        f"submission settled applied at tick {evidence['settle_tick']} "
        "with exactly one command_settled on the served and durable "
        "journals, both adopted receipt logs identical, and launch "
        f"roles held at tick {evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
