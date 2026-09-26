#!/usr/bin/env python3
"""The state-file sink-isolation leg for the reference plant — the
consumer-side proof that a stalled `--state-file` sink cannot lengthen
the scan on the deployed redundant pair (WW-ENG-003, WW-FND-004 — the
isolation contract the rig's state-file-isolation leg pins
in-workspace and on the rig, mirrored at the customer boundary where
the pinned release already carries it).

The controller captures a checkpoint under the executor lock at every
completed scan, but the serialization and write-then-rename drain on a
dedicated writer behind a bounded queue: a stalled mount can neither
lengthen a scan nor pin the serving lane, and the sink's named
`publication.state_sink` accounting must report the impediment rather
than let captures queue unboundedly. The leg launches the
manifest-declared pair on the released tooling through the shared
`pair.launch_pair` harness — each controller's declared `--state-file`
instantiated at a runner-owned scratch path — converges the standby to
`tracking`, then stalls the field owner's state-file sink exactly the
way the rig leg does: a reader-less FIFO staged at the sink's
write-then-rename temporary sibling (the `state.json.tmp` path beside
the declared state file), so the drain writer's next `open()` blocks
inside the mount while captures queue. The run:

- drives one batch of driven `POST /scan` requests on the field
  owner — every scan completing and publishing while the batch's
  durability-attesting answer parks on its own worker inside the
  declared bound — and proves through the serving lane that the served
  tick advances across the impeded window with `io_health` counters
  unmoved and the sink reporting its named `lagging` state bounded
  inside `capacity` — never `failed`, never an unbounded queue;
- holds the window and asserts the batch's attesting answer stands
  unanswered while the file cannot have caught up — the request
  worker's own bounded wait, never the scan's — while the serving
  lane's reads keep answering;
- restores the mount — a host reader pairing the writer's blocked
  `open()` so the stalled write completes and its rename carries the
  staged FIFO onto the state path until the next capture's regular
  temporary replaces it — asserting the parked answer returns `200`
  inside the bound, a fresh publication reports `healthy` with the
  queue empty and `lost` accounting nothing, and the durable file's
  persisted tick reached the run's last scan;
- reconverges the pair — one tracking-first driven tick aligning the
  standby back onto the owner's image — with the launch roles
  standing: the field owner `active`, its standby `tracking`.

Where the consumer harness admits no impediment lever — the field
owner declaring no `state_file`, the runner offering no `os.mkfifo`,
the staged node refused, or the served revision predating the
`state_sink`/`io_health` surface — the leg reports
`state-file-sink-isolation-digest inconclusive` rather than asserting.

Usage:

    state_file_sink_isolation.py --plant-server PATH \
        --controller PATH --model model/plant.json \
        --dynamics model/dynamics.json --scenario ci/scenario.json \
        --manifest deploy/manifest.json

On success one `state-file-sink-isolation-digest <sha256>` line prints
— the check runs two passes and compares them
(`state-file-sink-isolation-nondeterministic`). A contract violation
reports `state-file-sink-isolation: …` lines on stderr and exits 1 —
the check's `state-file-sink-isolation-failed`. `--tamper paced-scan`
doctors the leg's cadence expectation — asserting the stalled sink
must have lengthened the scan — so a conforming run fails it naming
the observed advance.
"""

import argparse
import hashlib
import json
import os
import stat
import sys
import threading
import time
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import pair


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored case: a leg asserting the impeded sink lengthened
# the scan must surface the named diagnostic — never a silently
# unexercised contract.
LEG = {
    "order": 340,
    "title": "the state-file sink-isolation leg",
    "passes": "state-file-sink-isolation",
    "tampers": [
        {
            "name": "paced-scan",
            "passed": "a paced-scan case passed the sink-isolation leg",
            "missed": "the paced-scan case did not report its named diagnostic",
            "evidence": ["the doctored expectation wanted the stalled sink"],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The deployed pair or the harness predates — or never admits —
    the contract the leg exercises: the run classifies inconclusive,
    never a product failure."""


# The driven scans the parked batch requests — past the first capture
# the blocked writer parks on, deep enough that the served stamp reads
# `lagging`, and far under the drain queue's declared bound a fuller
# queue would refuse at. The bounds: the batch's scans must publish
# inside the window bound, the attesting answer's parked hold proves
# the durability wait is the request's own, the answer itself gets a
# bound past the contract's drain wait once the mount restores, and
# every serving-lane read owes an answer inside the read bound while
# the sink stalls.
IMPEDED_SCANS = 6
WINDOW_BOUND = 15
POLL_INTERVAL = 0.025
HOLD_SECONDS = 1.0
ANSWER_BOUND = 40
SERVE_BOUND = 5.0

# The FIFO lever's own bounds: an in-flight regular temporary clears
# for the staged node inside the stage bound; once a reader pairs, the
# stalled write completes and the next queued capture's regular
# temporary restores the state path inside the drain bound; a writer
# that never attaches inside the attach grace means the staged node
# outlived its sink and is unlinked so the mount frees.
STAGE_BOUND = 10
DRAIN_BOUND = 15
ATTACH_GRACE = 3

# The io_health counters the cadence claim correlates — the stall must
# never surface as driver-boundary failure growth.
IO_COUNTER_KEYS = (
    "failed_reads",
    "failed_writes",
    "failed_exchanges",
    "consecutive_failures",
    "scan_overruns",
)


def is_fifo(path):
    """Whether `path` stands as a named pipe — the staged stall's node."""
    try:
        return stat.S_ISFIFO(os.lstat(path).st_mode)
    except OSError:
        return False


def state_sink(snapshot):
    """The snapshot's `publication.state_sink` section, or None — the
    served sink-health surface, absent on a revision that predates the
    contract."""
    return ((snapshot or {}).get("publication") or {}).get("state_sink")


def io_counters(snapshot):
    """The io_health counters the cadence claim correlates, or None
    when the section is absent."""
    health = (snapshot or {}).get("io_health")
    if health is None:
        return None
    return tuple(health.get(key) for key in IO_COUNTER_KEYS)


def window_get(url, what, failures):
    """One bounded serving-lane read through the impeded window — a
    read that cannot answer inside the declared bound names a pinned
    lane, the stall's other half."""
    try:
        with urllib.request.urlopen(url, timeout=SERVE_BOUND) as response:
            return json.load(response)
    except Exception as error:
        failures.append(f"{what} answered {error} while the sink stood stalled")
        raise Abort


def stage_fifo(tmp_path):
    """Stage the reader-less FIFO at the sink's write-then-rename
    temporary path — the drain writer's next `open()` blocks on the
    node while captures queue behind the bounded handoff. A capture's
    regular temporary already in flight clears on its own rename
    before the FIFO stages; an in-place FIFO makes the call
    idempotent. Raises OSError — TimeoutError included — when the
    mount refuses the staged node."""
    deadline = time.monotonic() + STAGE_BOUND
    while True:
        try:
            os.mkfifo(tmp_path)
            return
        except FileExistsError:
            if is_fifo(tmp_path):
                return  # the stall is already staged
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"a regular {tmp_path} never cleared for the staged FIFO"
                )
            time.sleep(0.01)


def release_fifo(tmp_path, state_path):
    """Release the stall `stage_fifo` staged on `tmp_path`: attach a
    reader to the staged FIFO so the drain writer's pending `open()`
    pairs, drain its bytes until the write's close+rename carries the
    FIFO onto the state path, then wait until a capture's regular
    temporary has made the state file an ordinary file again.

    Holding the read end pairs every writer open on the node — even a
    write that attached after the FIFO staged — so the mount heals
    without touching process or file identity. A readerless node whose
    grace lapses (a stalled drain's writer attaches inside a scan;
    only a dead sink never does) is unlinked so the mount frees.
    Raises RuntimeError when nothing was staged or a bounded wait
    lapses."""
    if not is_fifo(tmp_path):
        raise RuntimeError(f"no staged stall stands at {tmp_path}")
    fd = os.open(tmp_path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        attached = False
        bound = time.monotonic() + DRAIN_BOUND
        grace = time.monotonic() + ATTACH_GRACE
        while os.path.lexists(tmp_path):
            try:
                if os.read(fd, 1 << 16):
                    attached = True
            except BlockingIOError:
                attached = True  # a writer holds the node open
            if attached:
                if time.monotonic() > bound:
                    raise RuntimeError(
                        "the stalled state-file write never completed"
                    )
            elif time.monotonic() > grace:
                os.unlink(tmp_path)
                break
            else:
                time.sleep(0.005)
    finally:
        os.close(fd)
    # The renamed FIFO now answers as the state file until the next
    # queued capture's regular temporary replaces it — the mount is
    # restored once an ordinary file stands again.
    bound = time.monotonic() + DRAIN_BOUND
    while not (os.path.isfile(state_path) and not is_fifo(state_path)):
        if time.monotonic() > bound:
            raise RuntimeError(
                "the state path never returned to a regular file"
            )
        time.sleep(0.01)


def sink_isolation_pass(args, tamper):
    """The sink-isolation run: converge the declared pair, stall the
    field owner's state-file sink, prove the scans still publish and
    the named accounting reports the impediment, restore, and prove
    checkpointing resumes with the pair's launch roles standing.
    Returns `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the state-file "
            "sink-isolation leg has nothing to exercise"
        )
    _manifest, duty_decl, _standby_decl = declared
    digest_entries, evidence, failures = [], {}, []
    rig = None
    state_path = None
    tmp_path = None
    impeded = False
    answer = {"done": False}
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        state_path = rig.duty_files.get("state_file")
        if state_path is None:
            raise Inconclusive(
                "the manifest's field owner declares no state_file — "
                "the consumer harness admits no sink to impede"
            )
        if not hasattr(os, "mkfifo"):
            raise Inconclusive(
                "the consumer harness admits no impediment lever — "
                "the runner cannot stage a FIFO"
            )
        tmp_path = state_path + ".tmp"

        # Phase 1 — convergence and the baseline contract: the pair
        # leg's driven-tick loop rests the peers identical, the served
        # publication carries the sink-health surface, and every
        # earlier /scan's attestation leaves the queue drained.
        converged = rig.converge(failures)
        owner = converged["owner"]
        tick0 = owner["tick"]
        sink0 = state_sink(owner)
        if sink0 is None:
            raise Inconclusive(
                "the deployed revision predates the contract — the "
                "served publication carries no state_sink section"
            )
        counters0 = io_counters(owner)
        if counters0 is None:
            raise Inconclusive(
                "the served snapshot carries no io_health section"
            )
        evidence["converged"] = tick0
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
                "sink": sink0.get("state"),
            }
        )

        # Phase 2 — the stall: stage the reader-less FIFO at the
        # sink's write-then-rename temporary sibling so the drain
        # writer's next open() blocks inside the mount while captures
        # queue — never on the scan's lock.
        try:
            stage_fifo(tmp_path)
        except OSError as error:
            raise Inconclusive(
                f"the staged FIFO was refused "
                f"({type(error).__name__}) — the consumer harness "
                "admits no impediment lever"
            )
        impeded = True

        # Phase 3 — the impeded window: one parked batch of driven
        # scans on the field owner, each scan's capture queueing
        # behind the blocked writer while the batch's attesting answer
        # waits on its own worker. The leg's polls ride the serving
        # lane: the served tick must advance across the whole batch
        # while the io_health counters stand unmoved and the sink
        # reports only its named states.
        def batch():
            try:
                status, body = pair.request(
                    f"{duty_url}/scan", {"scans": IMPEDED_SCANS}
                )
                answer["status"], answer["body"] = status, body
            except Exception as error:
                answer["error"] = str(error)[:300]
            answer["done"] = True

        thread = threading.Thread(target=batch, daemon=True)
        thread.start()
        deadline = time.monotonic() + WINDOW_BOUND
        latest = None
        while True:
            latest = window_get(
                duty_url + "/snapshot", "GET /snapshot", failures
            )
            tick = latest.get("tick")
            if not isinstance(tick, int) or tick < tick0:
                failures.append(
                    "the served tick regressed under the stall"
                )
                raise Abort
            counters = io_counters(latest)
            if counters is None:
                failures.append(
                    "the io_health section vanished under the stall"
                )
                raise Abort
            for key, before, now in zip(IO_COUNTER_KEYS, counters0, counters):
                if now != before:
                    failures.append(
                        f"the stall surfaced as io_health.{key} drift "
                        f"({before} -> {now})"
                    )
                    raise Abort
            sink = state_sink(latest) or {}
            if sink.get("state") == "failed":
                failures.append(
                    "the stalled mount escalated to the sink's failed "
                    "state"
                )
                raise Abort
            if sink.get("state") not in ("healthy", "lagging"):
                failures.append(
                    f"the sink reports the unnamed state "
                    f"{sink.get('state')!r}"
                )
                raise Abort
            if tick >= tick0 + IMPEDED_SCANS:
                break
            if answer.get("done"):
                failures.append(
                    "the impeded window's scan batch answered before "
                    f"its scans published — {answer.get('status')} "
                    f"{answer.get('body', answer.get('error'))}"
                )
                raise Abort
            if time.monotonic() > deadline:
                failures.append(
                    "the served tick stopped advancing while the sink "
                    "stood stalled — the impeded mount paced the scan"
                )
                raise Abort
            time.sleep(POLL_INTERVAL)

        # The published window's verdict: the scans completed at their
        # own cadence while the sink's named accounting reported the
        # impediment — `lagging` with the queue bounded inside its
        # declared capacity, the captures accounted rather than
        # dropped unaccounted.
        sink = state_sink(latest) or {}
        depth = sink.get("depth")
        capacity = sink.get("capacity")
        if sink.get("state") != "lagging":
            failures.append(
                f"the stalled mount never surfaced the named lagging "
                f"state — the sink reports {sink.get('state')!r} after "
                "the impeded batch"
            )
            raise Abort
        if not isinstance(depth, int) or depth < 1 or (
            isinstance(capacity, int) and depth > capacity
        ):
            failures.append(
                f"the lagging sink reports depth {depth} against "
                f"capacity {capacity} — the impediment queued "
                "unboundedly or not at all"
            )
            raise Abort
        if sink.get("lost"):
            failures.append(
                "the stalled mount lost captures before any write "
                "failed"
            )
            raise Abort
        advanced = latest["tick"] - tick0
        if tamper == "paced-scan":
            # The doctored expectation — a sink the scan waits on: the
            # impeded mount must have lengthened the batch's scans.
            # A conforming run — the scans completing while the writer
            # parks — fails it, naming the observed advance.
            if advanced:
                failures.append(
                    "the doctored expectation wanted the stalled sink "
                    f"lengthening the scan — the served tick advanced "
                    f"{advanced} scans across the impeded window"
                )
                raise Abort
        # The parked hold: the batch's durability-attesting answer
        # must stand unanswered while the file cannot have caught up —
        # the request worker's own bounded wait, never the scan's —
        # with the serving lane's reads still answering inside theirs.
        hold = time.monotonic() + HOLD_SECONDS
        while time.monotonic() < hold:
            if answer.get("done"):
                failures.append(
                    "the impeded window's attesting answer completed "
                    "while the file could not have caught up"
                )
                raise Abort
            window_get(duty_url + "/role", "GET /role", failures)
            time.sleep(0.1)
        digest_entries.append(
            {
                "phase": "impeded",
                "scans": IMPEDED_SCANS,
                "advanced": advanced,
                "sink": sink.get("state"),
                "bounded": isinstance(capacity, int)
                and isinstance(depth, int)
                and depth <= capacity,
                "answer_parked": True,
                "io_health": "unchanged",
            }
        )
        evidence["impeded"] = latest["tick"]

        # Phase 4 — the restore: a host reader pairs the writer's
        # blocked open(), the stalled write completes, and the queued
        # captures drain in order until the state path is an ordinary
        # file again.
        try:
            release_fifo(tmp_path, state_path)
        except (OSError, RuntimeError) as error:
            raise Inconclusive(
                f"the sink's restore never completed "
                f"({type(error).__name__}) — the staged lever never "
                "released the mount"
            )
        impeded = False
        thread.join(ANSWER_BOUND)
        if not answer.get("done"):
            failures.append(
                "the impeded window's scan batch never answered after "
                "the mount restored"
            )
            raise Abort
        if answer.get("status") != 200:
            failures.append(
                f"the impeded window's scan batch answered "
                f"{answer.get('status')} "
                f"{answer.get('body', answer.get('error'))} after the "
                "mount restored, expected 200"
            )
            raise Abort
        body = answer.get("body") or {}
        if body.get("tick") != tick0 + IMPEDED_SCANS:
            failures.append(
                f"the impeded window's scan batch answered tick "
                f"{body.get('tick')}, expected {tick0 + IMPEDED_SCANS}"
            )
            raise Abort

        # Phase 5 — the resume: one fresh driven scan publishes the
        # drained sink's named health and its own attested answer
        # places the durable file at the run's tick.
        resumed = pair.scan(duty_url, failures)
        sink = state_sink(resumed) or {}
        if sink.get("state") != "healthy" or sink.get("depth") != 0:
            failures.append(
                f"the sink reports {sink.get('state')!r} depth "
                f"{sink.get('depth')} after the restore drained — "
                "expected healthy with an empty queue"
            )
            raise Abort
        if sink.get("lost"):
            failures.append("captures were lost across the stall")
            raise Abort
        try:
            with open(state_path) as handle:
                persisted = json.load(handle).get("tick")
        except (OSError, json.JSONDecodeError) as error:
            failures.append(
                f"the durable file does not parse after the restore: "
                f"{error}"
            )
            raise Abort
        if persisted != resumed["tick"]:
            failures.append(
                f"the durable file persisted tick {persisted} while "
                f"the run stands at {resumed['tick']} — checkpointing "
                "never resumed"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "restored",
                "answered": answer["status"],
                "sink": sink.get("state"),
                "lost": sink.get("lost"),
                "file_tick": persisted,
            }
        )
        evidence["resumed"] = resumed["tick"]

        # Phase 6 — the pair stands: one tracking-first driven tick
        # reconverges the standby onto the owner's image and the
        # launch roles report unchanged.
        _tracked, owner = rig.tick(standby_url, duty_url, failures)
        duty_role = pair.get(duty_url + "/role", "GET /role", failures)
        standby_role = pair.get(
            standby_url + "/role", "GET /role", failures
        )
        if duty_role.get("role") != "active":
            failures.append(
                f"the field owner reports {duty_role.get('role')!r} "
                "after the restore, expected active"
            )
        sync = standby_role.get("sync")
        if standby_role.get("role") != "standby" or not (
            isinstance(sync, dict) and "tracking" in sync
        ):
            failures.append(
                f"the tracking peer reports "
                f"{standby_role.get('role')!r}/{sync} after the "
                "restore, expected standby/tracking"
            )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "pair",
                "tick": owner["tick"],
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
        if impeded and tmp_path is not None:
            try:
                release_fifo(tmp_path, state_path)
            except Exception:
                pass  # the run's recorded failure stands; the rig's
                # own teardown outlives the orphaned node
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
        choices=["paced-scan"],
        help="doctor the leg's own expectation — the pass must fail "
        "naming the evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = sink_isolation_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                f"state-file-sink-isolation: the {args.tamper} case met "
                "an inconclusive run — the doctored case offers no "
                "evidence"
            )
            return 1
        eprint(f"state-file-sink-isolation: inconclusive — {inconclusive}")
        print(
            f"state-file-sink-isolation-digest inconclusive — "
            f"{inconclusive}"
        )
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"state-file-sink-isolation: {line}")
        return 1
    for failure in failures:
        eprint(f"state-file-sink-isolation: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"state-file-sink-isolation: the {args.tamper} case "
                "passed silently — the leg never noticed the doctored "
                "run"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"state-file-sink-isolation-digest {digest} — the stalled "
        f"mount held the sink's named lagging state bounded while "
        f"{IMPEDED_SCANS} driven scans published to tick "
        f"{evidence['impeded']} with io_health unchanged, the "
        f"attesting answer parked until the restore, the file "
        f"resumed at tick {evidence['resumed']}, and the pair's "
        "launch roles stood"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
