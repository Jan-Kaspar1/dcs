#!/usr/bin/env python3
"""The command-overflow submission-order leg for the reference plant —
the consumer-side proof that the deployed redundant pair's bounded
command ingress keeps minting receipts in submission order when the
HTTP command lane itself overflows (WW-ENG-003, WW-FND-004): the
consumer-boundary mirror of the rig's #1057 lane-overflow ordering
contract — a `POST /command` wave past the lane's declared queue bound
must defer at the dispatcher and re-enter at the lane's drained tail,
so the one command worker mints every receipt where its submission
landed rather than a second consumer racing the queued wave on the
shared lock.

The pair leg (`ci/legs/pair.py`) proves the manifest-declared pair runs
and switches; the command-admission leg proves the queue's bounded
verdicts inside the lane. This leg exercises the ordering the same
deployment owes a wave bigger than the lane can hold: a release
predating the contract split the overflowed work across a second
consumer — the refused submission ran the real admission path beside
the draining worker and minted its receipt ahead of earlier
submissions still queued in the lane — so the leg watches exactly that
boundary, `pair.launch_pair` spawning the manifest-declared
controllers on the released tooling and the flood landing while the
pair stands converged in driven mode. The run:

- converges the declared standby to `tracking` through the pair leg's
  driven-tick loop, then reads the field owner's served
  `command_queue.capacity` — the declared admission bound the labeled
  submission sequence crosses — and derives the lane's declared queue
  bound the wave must also pass, `LANE_QUEUE_DEPTH` plus twice the
  capacity;
- pins the lane's one worker: the first submission rides its own
  connection with its body deliberately half-sent — a padded declared
  `Content-Length` past the server's eager buffer keeps the rest on
  the wire — while the labeled wave, submissions past the lane's
  bound, pipelines in behind it on one keep-alive connection, the
  deepest queue the contract shapes;
- completes the stalled body and collects every answer: each
  submission must return a structured receipt — the admitted
  `accepted`, each submission past the queue's declared bound the
  named `queue_full` rejection carrying the bound and the refused
  target — never a drop, a bare fault, or a hang, and the minted
  record the owner's live `GET /receipts` serves must carry the
  labeled sequence in submission order, each receipt echoing its own
  submission;
- asserts the flood never reached the run: the served reads —
  `/snapshot`, `/role`, `/receipts` on both peers — answering while
  the queue stands full, the run's tick unmoved, the roles standing,
  and the first driven `POST /scan` after the flood landing the
  owner's tick exactly one boundary on, a deferred submission holding
  no scan;
- drains and audits: tracking-first pair ticks adopt the settled
  record onto the standby, both peers' receipt logs answering the same
  log — the flood still in submission order, the admitted half settled
  `applied`, the overflow terminal `queue_full` — the `command_queue`
  sections reporting the flood's admission metrics identically, and
  the pair left in its launch roles, the field owner `active`, the
  standby `tracking`.

The contract postdates the pinned v0.3.0 release: where the launched
tooling predates the deferred-overflow re-entry, the flood's own
evidence — a submission answering no receipted frame, receipts
answering or minting out of submission order, a labeled receipt
missing from the served log — is the pre-contract refuse-path shape,
and the leg reports inconclusive rather than asserting until the
manifest repins a release carrying the contract.

Usage:

    command_overflow_order.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `command-overflow-order-digest <sha256>` line prints —
the check runs two passes and compares them
(`command-overflow-order-nondeterministic`). A contract violation
reports `command-overflow-order: …` lines on stderr and exits 1 — the
check's `command-overflow-order-failed`. `--tamper expect-reversed`
doctors the leg's mint-order expectation — asserting the served log
minted the flood in reversed submission order — so the leg proves its
ordering assertion fires on the honest in-order record rather than
passing an unexercised contract.
"""

import argparse
import hashlib
import json
import os
import socket
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import pair
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored case.
# The doctored case: a leg asserting the minted record in reversed
# submission order must surface the named diagnostic on the honest
# in-order log — never a silently unexercised ordering contract.
LEG = {
    "order": 330,
    "title": "the command-overflow submission-order leg",
    "passes": "command-overflow-order",
    "tampers": [
        {
            "name": "expect-reversed",
            "passed": "a reversed-mint expectation passed the overflow-order leg",
            "missed": "the expect-reversed case did not report its named diagnostic",
            "evidence": ["the doctored expectation wanted reversed mint order"],
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


ACTOR = "ci-command-overflow-order"

# The released lane bound the wave must cross — `LANE_QUEUE_DEPTH`
# beside twice the served `command_queue.capacity` is the command
# lane's declared queue bound, and the deferred deck the contract
# re-enters through carries the same `LANE_QUEUE_DEPTH` bound — the
# leg's margin past the lane bound stays inside it, so every labeled
# submission is owed a receipted answer on either release line.
LANE_QUEUE_DEPTH = 64
OVERFLOW_MARGIN = 32

# The served bound past which the leg's flood volume cannot reach.
CAPACITY_LIMIT = 512

# The stalled submission's shape: the `reason` padding pushes the
# declared `Content-Length` past the server's eager buffer so the
# half-sent body stays on the wire and the one command worker pins
# inside the body read, and the settle windows give the dispatcher the
# stalled head's routing and the wave's pile-up before the body
# completes.
STALL_PADDING = 2048
STALL_SETTLE_S = 0.2
PILE_SETTLE_S = 0.5
REPLY_TIMEOUT_S = 30

# The tracking-first pair ticks the drain phase drives — comfortably
# past the adopted settlement's one-pull lag.
DRAIN_TICKS = 3


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
    """The leg's minted receipts out of a served receipt log —
    `(index, receipt)` per entry carrying the leg's `ACTOR-<index>`
    label, in the log's mint order."""
    found = []
    for entry in receipts:
        actor = entry.get("actor")
        if isinstance(actor, str) and actor.startswith(ACTOR + "-"):
            suffix = actor[len(ACTOR) + 1:]
            if suffix.isdigit():
                found.append((int(suffix), entry))
    return found


def post_head(length, close=False):
    """One `POST /command` request head — `length` the declared
    Content-Length, `close` marking the single-shot connection the
    stalled submission rides."""
    head = (
        "POST /command HTTP/1.1\r\n"
        "Host: ci\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {length}\r\n"
    )
    if close:
        head += "Connection: close\r\n"
    return head.encode() + b"\r\n"


def read_response(reader):
    """One HTTP response off a connection's buffered reader —
    `(status, decoded)` with the JSON body parsed where it parses —
    the pipelined wave's answers in connection order."""
    status_line = reader.readline()
    if not status_line.startswith(b"HTTP/"):
        raise ValueError(f"the reply stream opened {status_line!r}")
    status = int(status_line.split(None, 2)[1])
    headers = {}
    while True:
        line = reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        name, _, value = line.decode("latin-1").partition(":")
        headers[name.strip().lower()] = value.strip()
    declared = headers.get("content-length")
    if declared is None:
        raise ValueError(f"the reply stream carried no content-length: {status_line!r}")
    body = reader.read(int(declared))
    try:
        return status, json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return status, body.decode("utf-8", errors="replace")


def lane_wave(url, bodies):
    """The labeled flood against the field owner's `POST /command`:
    `bodies[0]` pinned half-sent on its own connection so the lane's
    one worker blocks inside the body read, the rest pipelined
    back-to-back on a second connection behind it — the wave landing
    past the lane's declared queue bound while nothing drains —
    then the stalled body completed and every answer collected.
    Returns `(stalled_reply, replies)`, each `(status, decoded)`."""
    address = url.removeprefix("http://")
    host, _, port = address.rpartition(":")
    stalled = socket.create_connection(
        (host, int(port)), timeout=REPLY_TIMEOUT_S
    )
    try:
        # The stall: the head plus the body's first half lands the
        # request on the lane and pins the worker inside the body read
        # — the padded declared length keeps the rest on the wire.
        body = bodies[0]
        split = len(body) // 2
        stalled.sendall(post_head(len(body), close=True) + body[:split])
        time.sleep(STALL_SETTLE_S)
        flood = socket.create_connection(
            (host, int(port)), timeout=REPLY_TIMEOUT_S
        )
        try:
            flood.sendall(
                b"".join(
                    post_head(len(frame)) + frame for frame in bodies[1:]
                )
            )
            # The wave piles into the lane and its overflow deck
            # behind the pinned head before the body completes and the
            # drain begins.
            time.sleep(PILE_SETTLE_S)
            stalled.sendall(body[split:])
            stalled_reply = read_response(stalled.makefile("rb"))
            reader = flood.makefile("rb")
            replies = [read_response(reader) for _ in bodies[1:]]
        finally:
            flood.close()
    finally:
        stalled.close()
    return stalled_reply, replies


def overflow_pass(args, tamper):
    """The overflow run: converge, stall, flood, minted record, held
    reads, drain, audit, roles. Returns `(digest_entries, evidence,
    failures)`; raises `Inconclusive` where the pinned release
    predates the lane-overflow ordering contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "command-overflow-order leg has nothing to exercise"
        )
    with open(args.model) as handle:
        model = json.load(handle)
    points = writable_bool_points(model)
    if not points:
        raise Abort(
            "the emitted model declares no writable boolean point — "
            "the command-overflow-order leg has nothing to flood"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url

        # Phase 1 — convergence, then the declared bounds the flood
        # must cross: the served `command_queue.capacity` the named
        # rejection reports, and the lane's queue bound beside it.
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
        lane_bound = LANE_QUEUE_DEPTH + 2 * capacity
        count = lane_bound + OVERFLOW_MARGIN + 1
        evidence["converged"] = pre_tick
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
                "capacity": capacity,
                "lane_bound": lane_bound,
            }
        )

        # Phase 2 — the flood: the labeled sequence, submission 0
        # pinning the lane's one worker inside its half-sent body
        # while the wave — past the lane's declared queue bound —
        # pipelines in behind it. The contract owes every submission a
        # receipted answer; a missing, unframed, or bare-fault answer
        # is the pre-contract refuse boundary, never a violation of
        # the ordering under test.
        submissions = []
        for index in range(count):
            envelope = {
                "command": flood_command(points, index),
                "actor": f"{ACTOR}-{index}",
            }
            if index == 0:
                envelope["reason"] = "x" * STALL_PADDING
            submissions.append(envelope)
        bodies = [json.dumps(envelope).encode() for envelope in submissions]
        try:
            stalled_reply, replies = lane_wave(duty_url, bodies)
        except (OSError, ValueError) as error:
            raise Inconclusive(
                "the flood's pipelined answers never returned one "
                f"framed response per submission — {error!r} — the "
                "pinned release predates the lane-overflow ordering "
                "contract"
            )
        answers = [stalled_reply] + replies
        for index, (status, receipt) in enumerate(answers):
            if (
                status != 200
                or not isinstance(receipt, dict)
                or not isinstance(receipt.get("outcome"), dict)
            ):
                raise Inconclusive(
                    f"the flood's submission {index} answered "
                    f"{status} {str(receipt)[:200]} — every submission "
                    "is owed a receipted answer; the pinned release "
                    "predates the lane-overflow ordering contract"
                )
        digest_entries.append(
            {
                "phase": "flood",
                "capacity": capacity,
                "lane_bound": lane_bound,
                "submissions": count,
            }
        )

        # Phase 3 — the minted record: the owner's live receipt mirror
        # already carries every verdict in mint order. The contract
        # under test is exactly that mint order — the deferred wave
        # re-entered at the lane's drained tail — so a log carrying
        # the labeled sequence out of submission order is the release
        # predating it, inconclusive rather than a violation; the
        # `expect-reversed` tamper doctors the leg's own expectation
        # so the honest record must fail it by name.
        held_receipts = pair.get(
            f"{duty_url}/receipts", "GET /receipts", failures
        )
        mine = flood_receipts(held_receipts)
        if len(mine) != count:
            raise Inconclusive(
                f"the served receipt log holds {len(mine)} of the "
                f"flood's {count} labeled receipts — a submission "
                "dropped without a receipt; the pinned release "
                "predates the lane-overflow ordering contract"
            )
        signature = [index for index, _receipt in mine]
        if tamper == "expect-reversed":
            if signature != list(reversed(range(count))):
                failures.append(
                    "the doctored expectation wanted reversed mint "
                    "order — the served log minted the labeled "
                    "sequence "
                    f"{signature[:4]}…{signature[-4:]}"
                )
                raise Abort
        elif signature != list(range(count)):
            raise Inconclusive(
                "the served receipt log minted the labeled flood out "
                "of submission order — the pinned release predates "
                "the lane-overflow ordering contract"
            )
        by_index = {index: receipt for index, receipt in mine}
        outcomes = [
            simulate.receipt_outcome(by_index[index])
            for index in range(count)
        ]
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
        for index, receipt in by_index.items():
            submission = submissions[index]
            if (
                receipt.get("command") != submission["command"]
                or receipt.get("actor") != submission["actor"]
            ):
                failures.append(
                    f"the minted receipt for submission {index} "
                    f"echoes {receipt} — the log's entry is not the "
                    "submission's own"
                )
                break
        for index, (_status, receipt) in enumerate(answers):
            submission = submissions[index]
            if (
                receipt.get("command") != submission["command"]
                or receipt.get("actor") != submission["actor"]
            ):
                failures.append(
                    f"the flood's answer {index} carries "
                    f"{receipt.get('actor')!r}'s receipt — the "
                    "submission was answered another submission's "
                    "receipt"
                )
                break
        reason = (
            by_index[capacity]
            .get("outcome", {})
            .get("rejected", {})
            .get("reason", {})
        )
        verdict = reason.get("queue_full") or {}
        if (
            verdict.get("capacity") != capacity
            or verdict.get("point")
            != submissions[capacity]["command"]["write_value"]["point"]
        ):
            failures.append(
                f"submission {capacity}'s rejection reads {reason} — "
                "the queue_full verdict must name the declared bound "
                "and the refused target"
            )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "minted",
                "outcomes": outcomes,
                "first_refusal": by_index[capacity],
            }
        )

        # Phase 4 — the held queue: the flood sits at the bound while
        # the served surface keeps answering — the reads, the run's
        # tick, and the roles must all stand unmoved.
        held = pair.get(f"{duty_url}/snapshot", "GET /snapshot", failures)
        held_duty = pair.get(f"{duty_url}/role", "GET /role", failures)
        held_standby = pair.get(
            f"{standby_url}/role", "GET /role", failures
        )
        pair.get(f"{standby_url}/snapshot", "GET /snapshot", failures)
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

        # Phase 5 — the drain: the first driven scan after the flood
        # lands the owner's tick exactly one boundary on — no deferred
        # submission held the scan — then tracking-first pair ticks
        # adopt the drained, settled line onto the standby.
        drained = pair.scan(duty_url, failures)
        if drained.get("tick") != pre_tick + 1:
            failures.append(
                f"the flood's drain scan landed at tick "
                f"{drained.get('tick')}, expected {pre_tick + 1} — a "
                "deferred submission delayed the scan boundary"
            )
            raise Abort
        evidence["drained"] = drained["tick"]
        owner = drained
        drain_ticks = [drained["tick"]]
        for _ in range(DRAIN_TICKS):
            _tracked, owner = rig.tick(standby_url, duty_url, failures)
            drain_ticks.append(owner["tick"])
        digest_entries.append({"phase": "drain", "ticks": drain_ticks})

        # Phase 6 — the audit: the admission metrics ride the served
        # `command_queue` section identically on both peers, the
        # adopted receipt logs are one log still carrying the flood's
        # mint order — the admitted half settled `applied`, the
        # overflow terminal `queue_full`.
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
            "attempts": baseline_attempts + count,
            "full_rejections": baseline_rejections + (count - capacity),
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
        if len(mine) != count:
            failures.append(
                f"the settled receipt log holds {len(mine)} flood "
                f"receipts, expected {count}"
            )
        else:
            settled_signature = [index for index, _receipt in mine]
            if settled_signature != list(range(count)):
                failures.append(
                    "the adopted receipt log no longer carries the "
                    "flood in submission order"
                )
            by_index = {index: receipt for index, receipt in mine}
            for index in range(count):
                want = "applied" if index < capacity else "queue_full"
                receipt = by_index[index]
                if (
                    receipt.get("command") != submissions[index]["command"]
                    or simulate.receipt_outcome(receipt) != want
                ):
                    failures.append(
                        f"the settled log's flood entry {index} "
                        f"reads {receipt}, expected submission "
                        f"{index}'s {want} — the flood did not "
                        "settle in submission order, or an overflow "
                        "settled"
                    )
                    break
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "audit",
                "duty_queue": duty_queue,
                "standby_queue": standby_queue,
                "settled": {
                    "applied": capacity,
                    "queue_full": count - capacity,
                },
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
        choices=["expect-reversed"],
        help="doctor the leg's mint-order expectation — the pass must "
        "fail naming the honest record",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = overflow_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "command-overflow-order: the doctored expectation "
                "wanted reversed mint order — an inconclusive run "
                "offers the doctored case no evidence"
            )
            return 1
        eprint(f"command-overflow-order: inconclusive — {inconclusive}")
        print(
            f"command-overflow-order-digest inconclusive — {inconclusive}"
        )
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"command-overflow-order: {line}")
        return 1
    for failure in failures:
        eprint(f"command-overflow-order: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"command-overflow-order: the {args.tamper} case "
                "passed silently — the leg never noticed the honest "
                "mint order"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"command-overflow-order-digest {digest} — tracking by tick "
        f"{evidence['converged']}, the {digest_entries[1]['submissions']}"
        f"-submission flood past the lane's declared bound minted in "
        f"submission order with {digest_entries[1]['capacity']} "
        f"admitted and the rest queue_full, the drain landing tick "
        f"{evidence['drained']} one boundary on, both peers' adopted "
        "logs identical, roles unchanged at tick "
        f"{evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
