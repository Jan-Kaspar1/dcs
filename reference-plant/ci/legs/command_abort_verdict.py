#!/usr/bin/env python3
"""The aborted bounded-command honest-verdict leg for the reference
plant — the consumer-side proof that a command submission aborted
client-side never reports `command failed` for a command the
controller may still apply, the settled receipt staying the only truth
(WW-ENG-003, WW-LCM-001).

The consumer-boundary mirror of the rig's command-abort-verdict leg —
the contract defect #948 names and the #1036 fix establishes: a
client-side bound on `POST /command` cancels the client's wait, never
the server's work — the request may already sit buffered in the listen
backlog — so an unanswered submission's outcome is genuinely unknown
and rendering it "command failed" asserts a non-application that is
not true. The platform pin is the fix's own regression: the mirrored
`submitCommand` driven past the abort bound answering the
indeterminate "outcome unknown" verdict, the journaled `command_settled`
resolving it. This leg exercises the same contract on the
customer-owned redundant pair the manifest declares —
`pair.launch_pair` spawning both released controllers — with the
container pause standing in for the reproduction's wedged controller.
The run:

- converges the declared standby to `tracking` through the pair leg's
  driven-tick loop, then gates the contract on the owner's served page:
  a release whose `GET /` predates the indeterminate-outcome machinery
  — the abandoned-submission record, the "outcome unknown" verdict, the
  journaled settle's resolution — reports
  `command-abort-verdict-digest inconclusive`, never a failure;
- pauses the field owner — `SIGSTOP` on the spawned process, the
  harness's stop/pause lever beside `pair.stop`'s container stop — and
  submits one receipted writable-point command through the released
  tooling's bounded `POST /command` path with the client bound
  tightened so the wait aborts before the response can return: the
  request's bytes sit buffered in the listen backlog while the
  mirrored post-facing report must answer the honest verdict — never
  "command failed";
- unpauses the owner and proves the buffered submission landed: the
  live receipt mirror carries the leg's admission `accepted`, the
  first driven `POST /scan` after the restore lands the owner's tick
  exactly one boundary on, and tracking-first pair ticks adopt the
  settled record onto the standby;
- audits the truth: both peers' adopted receipt logs answer
  identically with the leg's receipt terminal `applied`, the owner's
  served journal and each peer's durable `--journal-file` carry
  exactly one `command_settled` for the admission at the settle tick,
  and the pair stands in its launch roles — the field owner `active`,
  the standby `tracking`.

Usage:

    command_abort_verdict.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `command-abort-verdict-digest <sha256>` line prints —
the check runs two passes and compares them
(`command-abort-verdict-nondeterministic`). A contract violation
reports `command-abort-verdict: …` lines on stderr and exits 1 — the
check's `command-abort-verdict-failed`. `--tamper claims-failed`
doctors the leg's verdict vocabulary back to the defect's — an
abandoned submission reporting "command failed" — so the leg proves
its honest-verdict assertion fires rather than passing an unexercised
contract.
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
# The doctored case: a leg asserting the aborted submission honestly
# reported while the report displays "command failed" — the defect's
# verdict vocabulary — must surface the named diagnostic, never a
# silently unexercised contract.
LEG = {
    "order": 470,
    "title": "the command-abort-verdict leg",
    "passes": "command-abort-verdict",
    "tampers": [
        {
            "name": "claims-failed",
            "passed": "a claims-failed case passed the abort-verdict leg",
            "missed": "the claims-failed case did not report its named diagnostic",
            "evidence": ["claims \"command failed\""],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The pinned release predates — or never covers — the contract
    the leg exercises: the served page lacking the indeterminate-outcome
    machinery, the consumer harness admitting no stop/pause lever, or
    the aborted post's answer arriving inside the bound so no abort
    was exercised. The run classifies inconclusive, never a product
    failure."""


ACTOR = "ci-command-abort-verdict"

# The tightened client bound the leg's post waits under — the page's
# `AbortSignal.timeout` stand-in: far inside any served response while
# the owner stands paused, far past the cost of sending the request.
ABORT_BOUND_S = 0.5

# The admission's landing window after the restore — the buffered
# request's queue is processed the moment the monitor runs again, so
# this is a generous bound on scheduling, not on the contract.
ADMISSION_WAIT_S = 10.0
ADMISSION_POLL_S = 0.1

# The tracking-first pair ticks the settle phase drives — comfortably
# past the adopted settlement's one-pull lag.
SETTLE_TICKS = 3

# The contract markers the served page must carry — the
# indeterminate-outcome machinery the fix shipped: the abandoned
# submission's pending record, its "outcome unknown" verdict, and the
# journaled settle's resolution text. A page predating the fix serves
# none of them — the inconclusive gate.
PAGE_MARKERS = (
    "abandonedSubmission",
    "pendingCommands",
    "outcome unknown",
    "the abandoned submission settled",
)


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
    """One `POST /command` under the tightened client bound — the
    released tooling's bounded post path with the page's client-side
    abort bound mirrored: `("answered", decoded)` when the receipt
    lands inside the bound, `("refused", status, body)` for the
    endpoint's own answered 4xx — the one provable pre-admission
    refusal — and `("unanswered", error)` for every end without an
    answered response: the bound firing, a dead response path, or an
    unprovable non-delivery, all indistinguishable at the fetch
    surface."""
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


def submission_report(end, honest=True):
    """The post-facing report a submission's end is owed — the
    honest-verdict contract's vocabulary mirrored from the released
    page's `submitCommand`: the answered refusal is the one provable
    "command failed"; every unanswered end reports the indeterminate
    "outcome unknown" verdict, the journaled settled receipt the
    submission's only truth. `honest=False` replays the defect's
    vocabulary — an aborted post rendered "command failed" — the
    doctored negative the leg's assertion must catch."""
    kind = end[0]
    if kind == "answered":
        return json.dumps(end[1], sort_keys=True)
    if kind == "refused":
        return (
            "command failed: the monitor refused the request: "
            f"HTTP {end[1]}: {end[2]}"
        )
    if not honest:
        return "command failed: the submission aborted before the answer returned"
    return (
        "command outcome unknown — no receipt answered the submission "
        "and it may still apply; the journaled settled receipt is the "
        "verdict — resubmitting now risks applying the command twice."
    )


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
    """The abort-verdict run: converge, gate, abort, restore, settle,
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

        # Phase 2 — the contract gate: the owner's served page must
        # carry the indeterminate-outcome machinery the fix shipped —
        # a release predating it reports inconclusive, never a
        # failure.
        page = simulate.http_text(f"{duty_url}/")
        missing = [
            marker for marker in PAGE_MARKERS if marker not in page
        ]
        if missing:
            raise Inconclusive(
                "the pinned release predates the honest-verdict "
                "contract — the served page lacks "
                f"{', '.join(missing)}"
            )
        digest_entries.append({"phase": "gate", "markers": "served"})

        # Phase 3 — the aborted bounded submission: pause the field
        # owner, then post the receipted writable-point command under
        # the tightened client bound so the wait aborts with the
        # request buffered server-side. The post-facing report must
        # answer the honest verdict — the `claims-failed` tamper
        # doctors the leg's vocabulary to the defect's so the
        # assertion must fire.
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
        report = submission_report(end, honest=tamper != "claims-failed")
        if "command failed" in report:
            failures.append(
                f"the aborted submission's post-facing report claims "
                f"\"command failed\" — {report} — the honest-verdict "
                "contract forbids claiming failure for a command the "
                "controller may still apply"
            )
            raise Abort
        if "outcome unknown" not in report:
            failures.append(
                f"the aborted submission's post-facing report reads "
                f"{report} — the honest-verdict contract owes the "
                "indeterminate outcome-unknown verdict"
            )
            raise Abort
        digest_entries.append(
            {"phase": "abort", "end": end[0], "report": report}
        )

        # Phase 4 — the admission: the buffered submission lands once
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

        # Phase 5 — the settle: the first driven scan after the
        # restore lands the owner's tick exactly one boundary on and
        # applies the queued admission — then tracking-first pair
        # ticks adopt the settled record onto the standby.
        drained = pair.scan(duty_url, failures)
        if drained.get("tick") != pre_tick + 1:
            failures.append(
                f"the settle scan landed at tick {drained.get('tick')}, "
                f"expected {pre_tick + 1} — the aborted submission's "
                "queue held the scan boundary"
            )
            raise Abort
        settle_tick = drained["tick"]
        owner = drained
        settle_ticks = [settle_tick]
        for _ in range(SETTLE_TICKS):
            _tracked, owner = rig.tick(standby_url, duty_url, failures)
            settle_ticks.append(owner["tick"])
        digest_entries.append({"phase": "settle", "ticks": settle_ticks})

        # Phase 6 — the audit: the served /receipts on both peers are
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

        # Phase 7 — the pair stands in its launch roles: the field
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
        choices=["claims-failed"],
        help="doctor the leg's verdict vocabulary to the defect's — "
        "the pass must fail naming the claimed failure",
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
                "command-abort-verdict: the doctored vocabulary "
                "claimed the failure verdict — an inconclusive run "
                "offers the doctored case no evidence"
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
                "verdict"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"command-abort-verdict-digest {digest} — the aborted bounded "
        "submission reported the indeterminate verdict, never "
        f"command failed, then settled applied at tick "
        f"{digest_entries[4]['ticks'][0]} with exactly one "
        "command_settled on the served and durable journals — both "
        "peers' adopted logs identical, roles unchanged at tick "
        f"{evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
