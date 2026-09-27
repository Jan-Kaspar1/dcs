#!/usr/bin/env python3
"""The journal sink-isolation leg for the reference plant — the
consumer-side proof that a stalled `--journal-file` sink cannot
lengthen the scan on the deployed redundant pair (WW-ENG-003,
WW-FND-004 — #942's journal-append isolation contract, pinned
in-workspace and on the rig, mirrored at the customer boundary where
the manifest declares the per-controller journal mounts).

The recorder hands every journaled record to a bounded drain queue
under the executor lock while a dedicated writer appends them to the
journal file in `seq` order off the lock: a stalled sink can neither
lengthen a scan nor pin the serving lane, and the sink's named
`publication.journal_sink` accounting must report the impediment —
the named `lagging` state with the queue bounded inside `capacity` —
rather than let records queue unboundedly or drop unaccounted. The
leg launches the manifest-declared pair on the released tooling
through the shared `pair.launch_pair` harness — each controller's
declared `--journal-file` instantiated at a runner-owned scratch
path — converges the standby to `tracking`, and drives journaled
traffic (a receipted `write_value` settling `command_settled`, an
injected quality fault and its clear journaling `quality_changed`)
so the field owner's durable record is moving.

The impediment lever differs from the state-file sink's on purpose:
the state-file writer re-opens its write-then-rename temporary every
capture, so a reader-less FIFO at that path parks the next `open()`
inside the mount. The journal writer opens its file once at bind and
appends through the held descriptor — a FIFO at the declared path
can only block the bind-time replay, never a mid-run append, so the
consumer harness admits no filesystem lever on the journal path. The
leg instead parks the drain writer thread itself through the
runner's tracer capability — a `ptrace` attach on the journal sink's
`dcs-drain` writer, identified among the field owner's drain threads
by its observable effect: a journaled record queues (the submitting
request's durability-attesting answer parks) only while the journal
writer is held. The parked writer is the stalled-mount contract the
queue owes: accepted records wait in the bounded queue, durability-
attesting answers park on their own workers, and scans keep
publishing. The run:

- with the writer parked, drives one batch of driven `POST /scan`
  requests on the field owner — every scan completing and publishing
  while the batch's durability-attesting answer parks on its own
  worker inside the declared bound — beside a receipted command and
  an injected quality transition whose journaled records queue, and
  proves through the serving lane that the served tick advances
  across the impeded window with `io_health` counters unmoved and
  the sink reporting `lagging` bounded inside `capacity` — never
  `failed`, never an unbounded queue, `lost` accounting nothing;
- holds the window and asserts the batch's, the command's, and a
  `GET /journal` read's attesting answers stand unanswered while the
  file cannot have caught up — the request workers' own bounded
  wait, never the scan's — while the serving lane's reads keep
  answering;
- restores the mount — the tracer detach resuming the writer so the
  standing queue drains in `seq` order — asserting the parked
  answers return `200` inside the bound, a fresh publication reports
  `healthy` with the queue empty and `drained` reconciling
  `accepted`, and the durable file holds every record with `seq`s
  contiguous — no torn record, no duplicated record, nothing lost;
- reconverges the pair — one tracking-first driven tick aligning the
  standby back onto the owner's image — with the launch roles
  standing: the field owner `active`, its standby `tracking`.

Where the consumer harness admits no impediment lever — the field
owner declaring no `journal_file`, the runner offering no `/proc`
task surface or tracer attach, no `dcs-drain` writer answering to
the journal queue, or the served revision predating the
`journal_sink`/`io_health` surface — the leg reports
`journal-sink-isolation-digest inconclusive` rather than asserting.

Usage:

    journal_sink_isolation.py --plant-server PATH \
        --controller PATH --model model/plant.json \
        --dynamics model/dynamics.json --scenario ci/scenario.json \
        --manifest deploy/manifest.json

On success one `journal-sink-isolation-digest <sha256>` line prints —
the check runs two passes and compares them
(`journal-sink-isolation-nondeterministic`). A contract violation
reports `journal-sink-isolation: …` lines on stderr and exits 1 —
the check's `journal-sink-isolation-failed`. `--tamper paced-scan`
doctors the leg's cadence expectation — asserting the stalled sink
must have lengthened the scan — so a conforming run fails it naming
the observed advance.
"""

import argparse
import ctypes
import ctypes.util
import hashlib
import json
import os
import sys
import threading
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
# two digest-identical passes, and exercise its doctored cases.
# The doctored case: a leg asserting the impeded sink lengthened
# the scan must surface the named diagnostic — never a silently
# unexercised contract.
LEG = {
    "order": 390,
    "title": "the journal sink-isolation leg",
    "passes": "journal-sink-isolation",
    "tampers": [
        {
            "name": "paced-scan",
            "passed": "a paced-scan case passed the journal sink-isolation leg",
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


# The driven scans the parked batch requests — deep enough that the
# served stamp reads `lagging` while records wait behind the parked
# writer, and far under the drain queue's declared bound a fuller
# queue would refuse at. The bounds: the batch's scans must publish
# inside the window bound, the attesting answers' parked hold proves
# the durability wait is each request's own, the answers themselves
# get a bound past the contract's drain wait once the mount restores,
# and every serving-lane read owes an answer inside the read bound
# while the sink stalls.
IMPEDED_SCANS = 6
WINDOW_BOUND = 15
POLL_INTERVAL = 0.025
HOLD_SECONDS = 1.0
ANSWER_BOUND = 40
SERVE_BOUND = 5.0

# The tracer lever's own bounds: the attached writer must report its
# tracing-stop and the detached writer its resume inside the stop
# bound; the probe telling the journal writer from the sibling
# drains gets a bound to answer — it parks only while the journal
# writer itself is held.
STOP_BOUND = 5
PROBE_BOUND = 4

# The io_health counters the cadence claim correlates — the stall must
# never surface as driver-boundary failure growth.
IO_COUNTER_KEYS = (
    "failed_reads",
    "failed_writes",
    "failed_exchanges",
    "consecutive_failures",
    "scan_overruns",
)

ACTOR = "ci-journal-sink-isolation"

# The injected non-Good the fault surface carries — the same quality
# the pair legs' fault drives use.
BAD_QUALITY = {"bad": "device_fault"}

PTRACE_ATTACH = 16
PTRACE_DETACH = 17


def journal_sink(snapshot):
    """The snapshot's `publication.journal_sink` section, or None — the
    served sink-health surface, absent on a revision that predates the
    contract."""
    return ((snapshot or {}).get("publication") or {}).get("journal_sink")


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


def writable_bool_point(model):
    """The lowest-id writable boolean `in` point the emitted model
    declares — the receipted `write_value` target the traffic drives."""
    writable = [
        point["id"]
        for point in sorted(model["io_points"], key=lambda entry: entry["id"])
        if point.get("writable")
        and point["direction"] == "in"
        and point["value_type"] == "bool"
    ]
    return writable[0] if writable else None


def nonwritable_bool_point(model):
    """The lowest-id non-writable boolean `in` point — the refused
    `write_value` target whose settled receipt journals at submission,
    the probe record that names the journal sink's writer."""
    refused = [
        point["id"]
        for point in sorted(model["io_points"], key=lambda entry: entry["id"])
        if not point.get("writable")
        and point["direction"] == "in"
        and point["value_type"] == "bool"
    ]
    return refused[0] if refused else None


def journaled_input_point(model):
    """The lowest-id journaled `in` point — the quality-fault target
    whose transitions journal `quality_changed` on both peers."""
    journaled = [
        point["id"]
        for point in sorted(model["io_points"], key=lambda entry: entry["id"])
        if point.get("journaled") and point["direction"] == "in"
    ]
    return journaled[0] if journaled else None


def drain_threads(pid):
    """The controller's dedicated drain writers — the `dcs-drain`
    threads a `/proc` task surface exposes, in spawn order."""
    threads = []
    try:
        for entry in os.listdir(f"/proc/{pid}/task"):
            try:
                with open(f"/proc/{pid}/task/{entry}/comm") as handle:
                    if handle.read().strip() == "dcs-drain":
                        threads.append(int(entry))
            except (OSError, ValueError):
                continue
    except OSError:
        return []
    return sorted(threads)


def thread_state(tid):
    """The thread's one-letter state from `/proc/<tid>/stat` — `t` is
    the tracing-stop an attached writer reports."""
    with open(f"/proc/{tid}/stat") as handle:
        return handle.read().rsplit(")", 1)[1].split()[0]


def load_tracer():
    """The runner's tracer capability — the `ptrace` surface the
    impediment lever needs. Raises Inconclusive where the consumer
    harness cannot attach a writer."""
    if not os.path.isdir("/proc"):
        raise Inconclusive(
            "the consumer harness admits no impediment lever — no "
            "/proc task surface"
        )
    try:
        libc = ctypes.CDLL(
            ctypes.util.find_library("c") or "libc.so.6", use_errno=True
        )
        libc.ptrace.restype = ctypes.c_long
        libc.ptrace.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        return libc
    except (OSError, AttributeError) as error:
        raise Inconclusive(
            "the consumer harness admits no impediment lever — the "
            f"tracer call is unavailable ({error})"
        )


def ptrace(libc, request, tid):
    """One ptrace call against thread `tid`, raising OSError on the
    kernel's refusal."""
    if libc.ptrace(request, tid, None, None) == -1:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def park_writer(libc, tid):
    """Attach thread `tid` through ptrace and wait for its
    tracing-stop — the writer parked inside its sink operation, the
    stalled-mount contract the bounded queue owes. Raises OSError on
    a refused attach and RuntimeError when the stop never reports."""
    ptrace(libc, PTRACE_ATTACH, tid)
    deadline = time.monotonic() + STOP_BOUND
    while time.monotonic() < deadline:
        try:
            if thread_state(tid) == "t":
                return
        except OSError:
            break
        time.sleep(0.01)
    try:
        ptrace(libc, PTRACE_DETACH, tid)
    except OSError:
        pass
    raise RuntimeError(f"the attached writer {tid} never reported tracing-stop")


def release_writer(libc, tid):
    """Detach the parked writer — the mount's restore: the writer
    resumes its drain and the standing queue appends in push order.
    Raises OSError on a refused detach and RuntimeError when the
    writer never resumes."""
    ptrace(libc, PTRACE_DETACH, tid)
    deadline = time.monotonic() + STOP_BOUND
    while time.monotonic() < deadline:
        try:
            if thread_state(tid) != "t":
                return
        except OSError:
            return  # the thread is gone with its process
        time.sleep(0.01)
    raise RuntimeError(f"the detached writer {tid} never resumed")


def post(url, body):
    """POST `body` and return `(status, decoded)` — the answer a
    parked request carries once the mount restores."""
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(
            request, timeout=ANSWER_BOUND + WINDOW_BOUND
        ) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return error.code, raw.decode(errors="replace")


def get(url):
    """GET `url` and return `(status, decoded)` — the same answer
    shape the parked posts carry."""
    with urllib.request.urlopen(
        url, timeout=ANSWER_BOUND + WINDOW_BOUND
    ) as response:
        return response.status, json.load(response)


def park_request(answers, name, run):
    """Start request `run` on a daemon worker — its `(status, body)`
    answer or transport error lands in `answers[name]`, the holder the
    impeded window proves stands unanswered while the file is
    stalled."""
    answer = {"done": False}

    def work():
        try:
            answer["result"] = run()
        except Exception as error:
            answer["error"] = str(error)[:300]
        answer["done"] = True

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    answers[name] = (thread, answer)


def sink_isolation_pass(args, tamper):
    """The sink-isolation run: converge the declared pair, drive
    journaled traffic, park the field owner's journal writer, prove
    the scans still publish and the named accounting reports the
    impediment, release, and prove the standing queue drained in
    `seq` order with the pair's launch roles standing. Returns
    `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the journal "
            "sink-isolation leg has nothing to exercise"
        )
    with open(args.model) as handle:
        model = json.load(handle)
    write_point = writable_bool_point(model)
    refuse_point = nonwritable_bool_point(model)
    fault_point = journaled_input_point(model)
    if write_point is None or refuse_point is None or fault_point is None:
        raise Abort(
            "the emitted model declares no writable/refused/journaled "
            "boolean input — the journal sink-isolation leg has "
            "nothing to drive"
        )
    write_command = {
        "write_value": {
            "kind": "bool",
            "point": write_point,
            "value": {"bool": True},
        }
    }
    refuse_command = {
        "write_value": {
            "kind": "bool",
            "point": refuse_point,
            "value": {"bool": True},
        }
    }
    digest_entries, evidence, failures = [], {}, []
    rig = None
    impeded = False
    libc = None
    writer_tid = None
    answers = {}
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        journal_path = rig.duty_files.get("journal_file")
        if journal_path is None:
            raise Inconclusive(
                "the manifest's field owner declares no journal_file — "
                "the consumer harness admits no journal sink to impede"
            )
        libc = load_tracer()

        # Phase 1 — convergence and the baseline contract: the pair
        # leg's driven-tick loop rests the peers identical, the served
        # publication carries the sink-health surface, and every
        # earlier request's attestation leaves the queue drained.
        converged = rig.converge(failures)
        owner = converged["owner"]
        sink0 = journal_sink(owner)
        if sink0 is None:
            raise Inconclusive(
                "the deployed revision predates the contract — the "
                "served publication carries no journal_sink section"
            )
        counters0 = io_counters(owner)
        if counters0 is None:
            raise Inconclusive(
                "the served snapshot carries no io_health section"
            )
        evidence["converged"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
                "sink": sink0.get("state"),
            }
        )

        # Phase 2 — journaled traffic: a receipted write_value the
        # owner's next boundary settles into `command_settled`, and an
        # injected quality fault whose transitions journal
        # `quality_changed` on the field owner's durable record before
        # its clear journals the restoration — the flowing traffic the
        # impeded window's standing queue then holds.
        status, receipt = pair.request(
            f"{duty_url}/command", {"command": write_command, "actor": ACTOR}
        )
        if status != 200 or simulate.receipt_outcome(receipt) != "accepted":
            failures.append(
                f"the traffic's write_value answered {status} "
                f"{receipt}, expected an accepted receipt"
            )
            raise Abort
        rig.tick(standby_url, duty_url, failures)
        verdict = rig.plant_io.request(
            {
                "op": "inject_fault",
                "point": fault_point,
                "fault": {"quality": BAD_QUALITY},
            }
        )
        if verdict.get("result") != "done":
            failures.append(f"inject_fault answered {verdict}")
            raise Abort
        rig.tick(standby_url, duty_url, failures)
        verdict = rig.plant_io.request(
            {"op": "clear_fault", "point": fault_point}
        )
        if verdict.get("result") != "done":
            failures.append(f"clear_fault answered {verdict}")
            raise Abort
        rig.tick(standby_url, duty_url, failures)
        # Two quiesced driven scans: a publication stamps the sink at
        # publish — before its own attesting answer finishes the
        # drain — so the first quiet scan's stamp can still carry the
        # traffic's tail; the second publishes with nothing left to
        # journal and reads the drained drain.
        pair.scan(duty_url, failures)
        quiet = pair.scan(duty_url, failures)
        sink = journal_sink(quiet) or {}
        if sink.get("state") != "healthy" or sink.get("depth") != 0:
            failures.append(
                f"the journal sink reports {sink.get('state')!r} "
                f"depth {sink.get('depth')} under ordinary journaled "
                "traffic, expected healthy with an empty queue"
            )
            raise Abort
        accepted0 = sink.get("accepted")
        digest_entries.append(
            {
                "phase": "traffic",
                "tick": quiet["tick"],
                "accepted": accepted0,
            }
        )
        tick0 = quiet["tick"]

        # Phase 3 — the stall: park the field owner's journal drain
        # writer. The journal sink's `dcs-drain` is the one whose park
        # makes a journaled record queue — probed by a refused
        # write_value, which journals `command_settled` at submission
        # and parks its own durability-attesting answer only while the
        # journal writer itself is held; any other thread's park lets
        # the record drain and the answer return. The released
        # binary's bind order spawns the journal writer after the
        # state-file writer, so the latest drain thread probes first.
        candidates = drain_threads(rig.duty.pid)
        if not candidates:
            raise Inconclusive(
                "the field owner exposes no dcs-drain writer threads — "
                "the consumer harness admits no impediment lever"
            )
        for tid in reversed(candidates):
            try:
                park_writer(libc, tid)
            except (OSError, RuntimeError) as error:
                raise Inconclusive(
                    f"the runner cannot park the drain writer "
                    f"({error}) — the consumer harness admits no "
                    "impediment lever"
                )
            writer_tid = tid
            answers.pop("probe", None)
            park_request(
                answers,
                "probe",
                lambda: post(
                    f"{duty_url}/command",
                    {"command": refuse_command, "actor": ACTOR},
                ),
            )
            deadline = time.monotonic() + PROBE_BOUND
            while time.monotonic() < deadline:
                if answers["probe"][1].get("done"):
                    break
                time.sleep(0.05)
            if not answers["probe"][1].get("done"):
                break
            answers["probe"][0].join(ANSWER_BOUND)
            try:
                release_writer(libc, tid)
            except (OSError, RuntimeError) as error:
                raise Inconclusive(
                    f"the parked writer never resumed ({error}) — the "
                    "staged lever never released the mount"
                )
            writer_tid = None
        if writer_tid is None:
            raise Inconclusive(
                "no drain writer's park made a journaled record "
                "queue — the consumer harness admits no journal-path "
                "impediment lever"
            )
        impeded = True

        # Phase 4 — the impeded window: journaled traffic queues
        # behind the parked writer — an accepted command settling at
        # the batch's first scan, an injected fault's quality
        # transitions — while one driven scan batch's scans publish
        # and its attesting answer parks beside a `GET /journal`
        # read's, each on its own worker inside the declared bound.
        park_request(
            answers,
            "command",
            lambda: post(
                f"{duty_url}/command",
                {"command": write_command, "actor": ACTOR},
            ),
        )
        deadline = time.monotonic() + SERVE_BOUND
        while True:
            receipts = window_get(
                duty_url + "/receipts", "GET /receipts", failures
            )
            if any(
                entry.get("command") == write_command for entry in receipts
            ):
                break
            if time.monotonic() > deadline:
                failures.append(
                    "the impeded command's admission never published "
                    "its receipt"
                )
                raise Abort
            time.sleep(0.05)
        verdict = rig.plant_io.request(
            {
                "op": "inject_fault",
                "point": fault_point,
                "fault": {"quality": BAD_QUALITY},
            }
        )
        if verdict.get("result") != "done":
            failures.append(f"inject_fault answered {verdict}")
            raise Abort
        park_request(
            answers,
            "batch",
            lambda: post(f"{duty_url}/scan", {"scans": IMPEDED_SCANS}),
        )
        park_request(
            answers, "journal", lambda: get(f"{duty_url}/journal")
        )
        # The leg's polls ride the serving lane: the served tick must
        # advance across the whole batch while the io_health counters
        # stand unmoved and the sink reports only its named states.
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
            sink = journal_sink(latest) or {}
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
            if answers["batch"][1].get("done"):
                failures.append(
                    "the impeded window's scan batch answered before "
                    f"its scans published — "
                    f"{answers['batch'][1].get('result', answers['batch'][1].get('error'))}"
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
        # declared capacity, the records accounted rather than dropped
        # unaccounted.
        sink = journal_sink(latest) or {}
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
        if not isinstance(sink.get("accepted"), int) or not isinstance(
            accepted0, int
        ) or sink["accepted"] <= accepted0:
            failures.append(
                "the lagging sink's accepted counter did not move — "
                "the queued records were not accounted"
            )
            raise Abort
        if sink.get("lost"):
            failures.append(
                "the stalled mount lost records before any write "
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
        # The parked hold: the durability-attesting answers must stand
        # unanswered while the file cannot have caught up — the
        # request workers' own bounded wait, never the scan's — with
        # the serving lane's reads still answering inside theirs.
        hold = time.monotonic() + HOLD_SECONDS
        while time.monotonic() < hold:
            for name, (_thread, answer) in answers.items():
                if answer.get("done"):
                    failures.append(
                        f"the impeded window's {name} answer completed "
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

        # Phase 5 — the restore: the tracer detach resumes the writer,
        # the standing queue appends in `seq` order, and every parked
        # attesting answer returns inside the bound.
        try:
            release_writer(libc, writer_tid)
        except (OSError, RuntimeError) as error:
            raise Inconclusive(
                f"the writer never resumed ({error}) — the staged "
                "lever never released the mount"
            )
        impeded = False
        results = {}
        for name, (thread, answer) in answers.items():
            thread.join(ANSWER_BOUND)
            if not answer.get("done"):
                failures.append(
                    f"the impeded window's {name} answer never "
                    "returned after the mount restored"
                )
                raise Abort
            results[name] = answer.get(
                "result", ("transport", answer.get("error"))
            )
        status, body = results.get("batch") or (None, None)
        if status != 200:
            failures.append(
                f"the impeded window's scan batch answered "
                f"{results.get('batch')} after the mount restored, "
                "expected 200"
            )
            raise Abort
        if body.get("tick") != tick0 + IMPEDED_SCANS:
            failures.append(
                f"the impeded window's scan batch answered tick "
                f"{body.get('tick')}, expected {tick0 + IMPEDED_SCANS}"
            )
            raise Abort
        status, receipt = results.get("command") or (None, None)
        outcome = (
            simulate.receipt_outcome(receipt)
            if isinstance(receipt, dict) and "outcome" in receipt
            else None
        )
        if status != 200 or outcome != "accepted":
            failures.append(
                f"the impeded window's command answered "
                f"{results.get('command')}, expected an accepted "
                "receipt"
            )
            raise Abort
        status, refusal = results.get("probe") or (None, None)
        outcome = (
            simulate.receipt_outcome(refusal)
            if isinstance(refusal, dict) and "outcome" in refusal
            else None
        )
        if status != 200 or outcome in (None, "accepted", "applied"):
            failures.append(
                f"the probe command answered {results.get('probe')}, "
                "expected a refused receipt"
            )
            raise Abort
        status, served = results.get("journal") or (None, None)
        if status != 200 or not isinstance(served, list):
            failures.append(
                f"the impeded window's /journal read answered "
                f"{results.get('journal')}, expected the retained "
                "window"
            )
            raise Abort
        served_seqs = [entry.get("seq") for entry in served]
        if served_seqs != sorted(served_seqs) or len(set(served_seqs)) != len(
            served_seqs
        ):
            failures.append(
                "the retained journal did not answer in strict seq "
                "order after the restore"
            )
            raise Abort

        # Phase 6 — the resume: the injected fault clears, one driven
        # scan journals the restoration, and a quiesced publication
        # reports the drained sink's named health — the durable file
        # holding every accepted record in contiguous `seq` order, no
        # torn or duplicated record.
        verdict = rig.plant_io.request(
            {"op": "clear_fault", "point": fault_point}
        )
        if verdict.get("result") != "done":
            failures.append(f"clear_fault answered {verdict}")
            raise Abort
        pair.scan(duty_url, failures)
        pair.scan(duty_url, failures)
        resumed = pair.scan(duty_url, failures)
        sink = journal_sink(resumed) or {}
        if sink.get("state") != "healthy" or sink.get("depth") != 0:
            failures.append(
                f"the sink reports {sink.get('state')!r} depth "
                f"{sink.get('depth')} after the restore drained — "
                "expected healthy with an empty queue"
            )
            raise Abort
        if sink.get("lost"):
            failures.append("records were lost across the stall")
            raise Abort
        if sink.get("drained") != sink.get("accepted"):
            failures.append(
                f"the sink's drained counter {sink.get('drained')} "
                f"does not reconcile accepted {sink.get('accepted')} "
                "after the restore"
            )
            raise Abort
        try:
            records = pair.journal_records(journal_path)
        except Abort as abort:
            failures.append(
                f"the durable journal does not parse after the "
                f"restore: {'; '.join(str(arg) for arg in abort.args)}"
            )
            raise Abort
        entries = [record for kind, record in records if kind == "entry"]
        seqs = [entry["seq"] for entry in entries]
        if seqs != list(range(1, len(seqs) + 1)):
            failures.append(
                "the durable journal's entry seqs are not 1..n "
                f"contiguous after the restore: "
                f"{seqs[:8]}…{seqs[-4:] if seqs else []} — a record "
                "was torn, duplicated, or lost"
            )
            raise Abort
        if len(entries) != sink.get("accepted"):
            failures.append(
                f"the durable journal holds {len(entries)} entries "
                f"against the sink's accepted {sink.get('accepted')} — "
                "the standing queue did not drain whole"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "restored",
                "answered": results.get("batch", (None,))[0],
                "sink": sink.get("state"),
                "lost": sink.get("lost"),
                "file_entries": len(entries),
            }
        )
        evidence["resumed"] = resumed["tick"]

        # Phase 7 — the pair stands: one tracking-first driven tick
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
        if writer_tid is not None and libc is not None:
            try:
                release_writer(libc, writer_tid)
            except Exception:
                pass  # the run's recorded failure stands; the rig's
                # own teardown outlives the parked thread
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
                f"journal-sink-isolation: the {args.tamper} case met "
                "an inconclusive run — the doctored case offers no "
                "evidence"
            )
            return 1
        eprint(f"journal-sink-isolation: inconclusive — {inconclusive}")
        print(
            f"journal-sink-isolation-digest inconclusive — "
            f"{inconclusive}"
        )
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"journal-sink-isolation: {line}")
        return 1
    for failure in failures:
        eprint(f"journal-sink-isolation: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"journal-sink-isolation: the {args.tamper} case "
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
        f"journal-sink-isolation-digest {digest} — the stalled "
        f"mount held the sink's named lagging state bounded while "
        f"{IMPEDED_SCANS} driven scans published to tick "
        f"{evidence['impeded']} with io_health unchanged, the "
        f"attesting answers parked until the restore, the standing "
        f"queue drained in seq order to tick {evidence['resumed']}, "
        "and the pair's launch roles stood"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
