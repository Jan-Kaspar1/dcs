#!/usr/bin/env python3
"""The pending-serving-bound leg for the reference plant — the
consumer-boundary mirror of the simulated QA rig's
`pending_serving_bound` leg (#1324's contract), pinned on the
manifest-declared redundant pair and never before on the
customer-owned deployment: a pending born-active under a
connectable-but-silent field keeps every **lock-taking** serving
endpoint bounded instead of serializing the scan's field timeouts
under the executor lock (WW-ENG-003, WW-LCM-001).

The finding's shape is a wall-clock-paced run whose monitor shares one
executor lock with its scan. Against a field that completes the
handshake and then never answers, the scan held that lock across every
remote channel's full exchange timeout, so the checkpoint pull a
tracking consumer's failover heartbeat counts on, the two switchover
actuations an incident-time operator would issue, and the receipted
command path all queued behind the wedged scan — tens of seconds per
endpoint — while the lock-free published mirrors kept answering and
the served tick collapsed to a fraction of its configured cadence. A
failed exchange now arms the re-attach window, so the pending run's
scan stalls once near one field timeout per window instead of once per
channel, and the serving lane stays responsive for exactly the
incident-time use it exists for. This leg proves that on the shipped
endpoints, over the deployed pair, with the manifest's own model:

- the manifest-declared pair converges first — the field owner
  `active`, the declared standby `tracking` it — so the staged seat
  runs beside a settled deployment, not instead of one;
- the labeled born-active launches `--remote` at a **frozen field**: a
  listener that completes every handshake and answers nothing, the
  accept-but-never-answer shape the finding records. A
  connection-refused field fails its first access instantly and never
  queues a scan; this one is deliberately connectable. The launch
  stands pending — `standby`, `unsynchronized`, no observed claim —
  the run's own startup log naming the pending report;
- through the pending seat's serving monitor, across the declared
  frozen window: every lock-taking endpoint answers inside its
  declared bound near one field timeout with its **named** verdict —
  `GET /checkpoint` its document, `POST /promote` the `not_converged`
  refusal, `POST /demote` `not_active`, `POST /command` the receipted
  `not_active` rejection — never the recorded serialized stall, while
  the lock-free `GET /role` and `GET /health` mirrors keep answering
  instantly, the served tick holds its bounded degraded cadence, and
  the served `last_scan_age_ms` stays inside its own ceiling. The
  pending surface stays honest throughout: `standby` under an honest
  sync verdict with no held claim, and the served checkpoint stamping
  `source_owns_field` false — a pending run serves state and claims
  nothing;
- the thaw: the frozen field is released and a real
  `dcs-plant-server` binds the same address, and the first answered
  contact re-issues the deferred conditional grant per the standing
  born-active contract — the seat walks `standby` → `promoting` →
  `active` under the reclaim origin with the claim `held`, the walk
  journaled in its own durable file;
- the launch set restored: the staged seat and the staged field are
  gone, and the deployed pair still rests on its launch roles — one
  field-owning `active`, one `tracking` standby — for the legs behind
  this one.

The contract postdates the released artifact sets: an artifact set cut
before it reads this way until a release carries it. Where the staged
release exits the pending launch on a field-side startup condition,
serves a `/health` that never carries the bounded liveness report, or
shows the recorded defect signature itself — the honest pending
surface whose scan froze across the window while the executor's
serialized field stalls starved the lock-taking set — the leg reports
`pending-serving-bound-digest inconclusive` rather than asserting.

Usage:

    pending_serving_bound.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `pending-serving-bound-digest <sha256>` line prints —
the check runs two passes and compares them
(`pending-serving-bound-nondeterministic`); the leg stem is its file
name with underscores turned to dashes, so the failed diagnostic is the
check's `pending-serving-bound-failed`. A contract violation reports
`pending-serving-bound: …` lines on stderr and exits 1. The doctored
case `--tamper expect-serialized` flips the leg's own expectation to
the recorded defect — demanding that the lock-taking endpoints queue
the wedged scan's serialized field timeouts — so the honest bounded
run must fail naming the responses it observed instead, proving the
bounded-serving assertion fires rather than passing unexercised.
"""

import argparse
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import driver_recovery
import failover
import foreign_claim_release
import pair
import simulate
import stranded_rejoin


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its two
# digest-identical passes, and exercise its doctored case. No
# `failed` override is declared: the stem `pending-serving-bound` is
# the diagnostic prefix this leg's failures share.
# The doctored case: a leg asserting the recorded defect — every
# lock-taking endpoint queueing the wedged scan's serialized field
# timeouts — must surface the named diagnostic on the honest bounded
# run rather than passing an unexercised contract.
LEG = {
    # The next free slot after the release-class legs origin/main
    # added (demote-release-stays-released 750) — the stage runs the
    # legs in this order and no two may share one.
    "order": 760,
    "title": "the pending-serving-bound leg",
    "passes": "pending-serving-bound-leg",
    "tampers": [
        {
            "name": "expect-serialized",
            "passed": "an expect-serialized case passed the pending-serving-bound leg",
            "missed": "the expect-serialized case did not report its named diagnostic",
            "evidence": [
                "the doctored expectation wanted the lock-taking endpoints to queue behind the wedged scan",
            ],
        },
    ],
}


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort
# The inconclusive classification the sibling legs share — carried as
# `(reason, detail)`: `reason` the stable phrase the `inconclusive`
# digest line prints (two passes must share it), `detail` the run's own
# verdicts, reported on stderr only, where minted owner tokens and
# ephemeral endpoints belong.
Inconclusive = foreign_claim_release.Inconclusive


# The doctored expectation's stable evidence prefix — every failure
# the tampered pass records carries it, so the check's negative case
# finds it whether the honest run kept the serving lane bounded or a
# predating release offered the case no evidence at all.
TAMPER_EVIDENCE = (
    "the doctored expectation wanted the lock-taking endpoints to queue "
    "behind the wedged scan"
)


# The remote driver's own bounds as the released field driver documents
# them: five seconds for each request's write and response wait, one
# second of minimum spacing between re-attach attempts. The leg's
# declared bounds are stated against them, so a reader can see what
# "near one field timeout" costs on the released tooling.
FIELD_TIMEOUT = 5.0
REATTACH_INTERVAL = 1.0

# The serving bounds the pending seat owes across the frozen window, the
# platform's own statement of them: the controller crate's frozen-field
# leg and the QA rig's leg both hold every lock-taking request to one
# field timeout plus four seconds of slack — the scan a caller may queue
# behind stalls once near one timeout per re-attach window on a fixed
# revision, nowhere near the recorded per-channel serialization — and
# the lock-free mirrors, `GET /role` and `GET /health`, to a second.
MIRROR_BOUND = 1.0
LOCK_BOUND = FIELD_TIMEOUT + 4.0
# The served scan age's ceiling: one field timeout plus the crate's three
# seconds of slack — the paced scan keeps completing near one timeout per
# re-attach window, where the defect let the age run for tens of seconds.
SCAN_AGE_BOUND = int(1000 * (FIELD_TIMEOUT + 3.0))
# The floor the degraded cadence must clear across the window: the scan
# keeps completing, and a run whose tick stands still while the serving
# set starves is the recorded defect, not a slow machine.
TICK_FLOOR = 4
# The frozen observation window — the QA rig's leg's declared span,
# several bounded scans on a field that never answers and long enough
# that the four lock-taking endpoints each get their say inside the
# loop's rotation, while staying well under the defect's one ~70s scan.
FROZEN_WINDOW = 20.0
WINDOW_POLL = 0.4
# The wall-clock bound on the pair settling, on each wait the leg
# drives, and on the staged seat's serving.
SERVE_SETTLE = 30.0

# The paced schedule the staged seat runs — the controller's own
# documented default, the wall-clock pacing the finding's scan had.
PENDING_SCAN_MS = 100

# The settle train proving the pair's launch roles on the restore.
SETTLE_TICKS = 3

ACTOR = "ci-pending-serving-bound"

# The stderr vocabulary a launch's field-side exit names — a pinned
# release predating the born-active pending state dies here rather than
# standing. An exit naming one of these is that predating shape; an exit
# naming nothing is a defect.
PREDATING_WORDS = (
    "cannot connect",
    "claim",
    "rejoin",
    "write-ownership",
    "unknown option",
    "unrecognized",
)

# The sync verdicts an honest pending standby may report — `tracking` is
# only honest behind a field-owning source.
HONEST_SYNC = ("unsynchronized", "degraded", "orphaned", "diverged",
               "reinitialized")

# The lock-free published-mirror reads — they must answer instantly even
# while the executor's lock is held by a stalled scan.
MIRROR_PATHS = ("/role", "/health")

# The lock-taking serving set the pending contract owes bounded answers
# on — `(method, path, expected status, named verdict)`: the checkpoint
# pull a tracking consumer's failover heartbeat counts on, the two
# switchover actuations an incident-time operator would issue, and the
# receipted command path.
LOCK_CALLS = (
    ("GET", "/checkpoint", 200, "document"),
    ("POST", "/promote", 409, "not_converged"),
    ("POST", "/demote", 409, "not_active"),
    ("POST", "/command", 200, "rejected:not_active"),
)

# The recorded defect's own floor — one field timeout per declared
# channel, serialized under the executor lock. The doctored expectation
# demands every lock-taking endpoint have queued at least this much
# behind the wedged scan; the honest bounded run answers in a fraction
# of it and cannot satisfy the demand.
SERIALIZED_FLOOR = 60.0


class FrozenField:
    """The connectable-but-silent field: a listener that completes every
    handshake and answers nothing. The finding's shape needs exactly
    this — a connection-refused field fails its first access instantly
    and never queues a scan, while a frozen plant process still
    completes handshakes at the kernel and leaves every in-flight
    exchange unanswered. Accepted sockets are drained so a paced
    client's writes never wedge on a full buffer; the leg's subject is
    the *absence* of answers, which draining preserves. `thaw` releases
    the address — the held links reset rather than closed, leaving no
    server-side TIME_WAIT — so a real `dcs-plant-server` can rebind the
    same address, the field's return the deferred grant re-issues
    on."""

    def __init__(self, address):
        host, _, port = address.rpartition(":")
        self.address = (host, int(port))
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(self.address)
        listener.listen(64)
        self._listener = listener
        self._held = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    @property
    def address_text(self):
        """The frozen field's `host:port` — what the staged launch's
        `--remote` names and what the field's return rebinds."""
        return f"{self.address[0]}:{self.address[1]}"

    def _drain(self, connection):
        try:
            while True:
                if not connection.recv(65536):
                    break
        except OSError:
            pass

    def _accept(self):
        self._listener.settimeout(0.2)
        while not self._stop.is_set():
            try:
                connection, _ = self._listener.accept()
            except OSError:
                continue
            self._held.append(connection)
            threading.Thread(
                target=self._drain, args=(connection,), daemon=True
            ).start()

    def thaw(self):
        """Release the endpoint — the listener stops accepting and every
        held link is reset, freeing the address for the field's return."""
        self._stop.set()
        for connection in self._held:
            try:
                connection.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_LINGER,
                    struct.pack("ii", 1, 0),
                )
            except OSError:
                pass
            connection.close()
        self._thread.join(timeout=2)
        self._listener.close()
        self._held = []


def spawn_pending(args, field_addr, files):
    """Spawn the labeled born-active on the deployed pair's launch shape,
    paced: `dcs-controller <model> --remote <field> --listen --scan-ms
    100`, with the launched run's durable journal under the rig's
    runner-owned scratch. Pacing is the subject — the scan runs on the
    wall clock behind the monitor's executor lock, which is where the
    recorded serialization happened. Returns `(process, monitor_url,
    preamble)` like `pair.spawn_peer`: `monitor_url` None when the
    process exits before reporting a listener, the preamble then
    carrying the startup refusal's stderr lines."""
    argv = [
        args.controller,
        args.model,
        "--remote",
        field_addr,
        "--listen",
        "127.0.0.1:0",
        "--scan-ms",
        str(PENDING_SCAN_MS),
        "--dt",
        str(args.dt),
    ]
    for field, flag in (
        ("journal_file", "--journal-file"),
        ("state_file", "--state-file"),
    ):
        if files.get(field) is not None:
            argv += [flag, files[field]]
    process = subprocess.Popen(
        argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
    )
    preamble = []
    for line in process.stderr:
        line = line.strip()
        if "listening on" in line:
            return (
                process,
                "http://" + pair.dialable(line.rsplit(None, 1)[-1]),
                preamble,
            )
        preamble.append(line)
    process.wait(timeout=10)
    return process, None, preamble


def spawn_pending_or_classify(args, field_addr, files):
    """`spawn_pending` plus the release gate: under the standing
    born-active contract a launch against a field that cannot be asked
    never exits for that reason — it stands pending — so an exit naming
    the field-side refusal vocabulary is a pinned release predating the
    contract and reports inconclusive, while an exit naming nothing is
    a defect."""
    process, url, preamble = spawn_pending(args, field_addr, files)
    if url is not None:
        return process, url, preamble
    joined = " ".join(preamble)
    if any(word in joined for word in PREDATING_WORDS):
        raise Inconclusive(
            "the labeled born-active exited rather than standing "
            "pending on the frozen field — the pinned release predates "
            "the born-active pending contract",
            "its startup log read: "
            + ("; ".join(preamble[-2:]) or "no diagnostic"),
        )
    raise Abort(
        "the labeled born-active exited at startup without naming a "
        "field-side refusal: "
        + ("; ".join(preamble) or "no diagnostic")
    )


def scratch_files(rig, name):
    """The staged seat's durable journal under the rig's runner-owned
    scratch — the file the deferred grant's landing is recorded in."""
    root = os.path.join(rig.scratch, name)
    os.makedirs(root, exist_ok=True)
    return {"journal_file": os.path.join(root, "journal.jsonl")}


def spawn_plant_at(args, address):
    """A `dcs-plant-server` bound at `address` — the field's return, the
    contact the deferred conditional grant re-issues on."""
    process = subprocess.Popen(
        [
            args.plant_server,
            args.model,
            "--dynamics",
            args.dynamics,
            "--listen",
            address,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    bound = simulate.listen_address(process, "dcs-plant-server")
    if bound != address:
        pair.stop(process)
        raise Abort(
            f"the returning field bound {bound}, not the frozen field's "
            f"{address} — the deferred grant cannot land"
        )
    return process


def serve(method, url, body, bound):
    """`(status, decoded)` for one serving request bounded by the
    caller's own patience — the pending surface's named refusals are
    decoded rather than raised, and a request queued past the bound
    raises, so the read is recorded unanswered rather than answering
    late."""
    data = None if method == "GET" else json.dumps(body or {}).encode()
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=bound) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            payload = raw.decode(errors="replace")
        finally:
            error.close()
        return error.code, payload


def verdict_of(path, payload):
    """The named verdict an answered serving call carries — the
    checkpoint's document, a switchover's named refusal, or the
    receipted command's settled outcome."""
    if path == "/checkpoint":
        return "document" if isinstance(payload, dict) else "malformed"
    if path == "/command":
        if not isinstance(payload, dict) or "outcome" not in payload:
            return "malformed"
        try:
            return "rejected:" + simulate.receipt_outcome(payload)
        except (KeyError, StopIteration, TypeError):
            return "malformed"
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict) and len(payload) == 1:
        return next(iter(payload))
    return "malformed"


def timed(base, method, path, bound, body=None):
    """One timed serving request — the read answers inside its bound or
    the client's own patience raises, so an endpoint queued behind the
    wedged scan records unanswered, never a late verdict."""
    read = {
        "method": method,
        "path": path,
        "bound": bound,
        "answered": False,
    }
    started = time.monotonic()
    try:
        status, payload = serve(method, base + path, body, bound)
    except Exception as error:
        read["error"] = str(error)[:150]
        read["elapsed"] = round(time.monotonic() - started, 4)
        return read
    read.update(
        {
            "answered": True,
            "status": status,
            "elapsed": round(time.monotonic() - started, 4),
            "verdict": verdict_of(path, payload),
        }
    )
    if isinstance(payload, dict):
        for key in (
            "role",
            "tick",
            "sync",
            "field_claim",
            "live",
            "last_scan_age_ms",
            "source_owns_field",
        ):
            if key in payload:
                read[key] = payload[key]
    return read


def sync_of(value):
    """The served StandbySync's variant name — `unsynchronized` is a
    bare string, the rest are single-key objects."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and value:
        return next(iter(value))
    return None


def health_report(read):
    """Whether a `/health` answer carries the bounded liveness report the
    contract serves — live, a role, an integer tick, and the
    completed-scan stamp field. A run that has not completed a scan yet
    serves that stamp null, the documented shape before the first
    completion, so only a run that never carries the field at all is
    read as a predating release."""
    if "last_scan_age_ms" not in read:
        return False
    stamp = read["last_scan_age_ms"]
    return (
        read.get("live") is True
        and isinstance(read.get("role"), str)
        and isinstance(read.get("tick"), int)
        and not isinstance(read.get("tick"), bool)
        and (
            stamp is None
            or (isinstance(stamp, int) and not isinstance(stamp, bool))
        )
    )


def integers(values):
    """The integer entries of a sampled sequence — a run's tick and
    scan-age samples, read through a JSON surface that carries nulls
    before the run's first completed scan."""
    return [
        value
        for value in values
        if isinstance(value, int) and not isinstance(value, bool)
    ]


def frozen_window(base, command):
    """The frozen window's evidence: the lock-free mirrors at the poll
    cadence and one rotating lock-taking call per round, the four
    rotating so each endpoint gets its say. The window runs at least
    `FROZEN_WINDOW`; past it the loop continues only until every lock
    call has been reached once, each probe self-bounding, so the tail
    stays bounded."""
    every = {call[1] for call in LOCK_CALLS}
    window = {
        "window_s": FROZEN_WINDOW,
        "reads": [],
        "ticks": [],
        "ages": [],
        "views": [],
        "probed": [],
    }
    deadline = time.monotonic() + FROZEN_WINDOW
    hard = deadline + SERVE_SETTLE
    rounds = 0
    probed = set()
    while time.monotonic() < deadline or (
        time.monotonic() < hard and probed != every
    ):
        for path in MIRROR_PATHS:
            read = timed(base, "GET", path, MIRROR_BOUND)
            read.update({"kind": "mirror", "want_status": 200})
            window["reads"].append(read)
            if not read.get("answered"):
                continue
            if path == "/role":
                window["ticks"].append(read.get("tick"))
                window["views"].append(
                    {
                        "role": read.get("role"),
                        "sync": sync_of(read.get("sync")),
                        "field_claim": read.get("field_claim"),
                    }
                )
            else:
                window["ages"].append(read.get("last_scan_age_ms"))
        method, path, status, verdict = LOCK_CALLS[rounds % len(LOCK_CALLS)]
        read = timed(
            base,
            method,
            path,
            LOCK_BOUND,
            body=command if path == "/command" else None,
        )
        read.update(
            {
                "kind": "lock",
                "want_status": status,
                "want_verdict": verdict,
            }
        )
        window["reads"].append(read)
        probed.add(path)
        rounds += 1
        time.sleep(WINDOW_POLL)
    window["probed"] = sorted(probed)
    return window


def predating(window):
    """The staged revision's pre-contract signature as `(reason,
    detail)`, or None when the record is the leg's to judge. Narrow by
    contract: a launch that stands while its monitor never serves is
    an instability the leg names rather than a predating release — the
    pending state has always published its mirror — while an exit, a
    `/health` that never carries the liveness report, and the recorded
    defect signature itself are the predating shapes."""
    reads = window.get("reads") or []
    answered = [read for read in reads if read.get("answered")]
    if not answered:
        return None
    health = [read for read in answered if read["path"] == "/health"]
    if health and not any(health_report(read) for read in health):
        return (
            "the pending seat's /health answers never carried the "
            "bounded liveness report — the pinned release predates the "
            "liveness contract the leg reads through",
            f"its /health answers were "
            f"{[json.dumps(read)[:120] for read in health[:2]]}",
        )
    views = window.get("views") or []
    coherent = bool(views) and all(
        view.get("role") == "standby"
        and view.get("field_claim") != "held"
        and view.get("sync") in HONEST_SYNC
        for view in views
    )
    ticks = integers(window.get("ticks") or [])
    ages = integers(window.get("ages") or [])
    starved = any(
        read.get("kind") == "lock"
        and (
            not read.get("answered")
            or (read.get("elapsed") or 0) > read.get("bound", LOCK_BOUND)
        )
        for read in reads
    )
    unbounded = bool(ages) and max(ages) > SCAN_AGE_BOUND
    frozen = bool(ticks) and ticks[-1] - ticks[0] < TICK_FLOOR
    if coherent and frozen and (starved or unbounded):
        return (
            "the pending attachment froze its scan across the frozen "
            "window while the executor's serialized field stalls "
            "starved the lock-taking serving set — the recorded defect "
            "signature: the pinned release predates the pending "
            "bounded-serving contract",
            f"its ticks ran {ticks}, its lock calls starved {starved}, "
            f"and its served scan ages reached "
            f"{max(ages) if ages else None}ms",
        )
    return None


def judge_window(window, failures, tamper):
    """The frozen window's contract clauses — the pending surface's
    honesty, the bounded degraded cadence, and every lock-taking
    endpoint's answer inside its declared bound with its named
    verdict."""
    reads = window.get("reads") or []
    if not any(read.get("answered") for read in reads):
        failures.append(
            "the pending seat's monitor answered no read inside the "
            "frozen window — the serving lane starved outright"
        )
        return
    for index, view in enumerate(window.get("views") or []):
        if view.get("role") != "standby":
            failures.append(
                f"view {index}: the pending seat reported a role it "
                f"cannot hold — {json.dumps(view)[:200]}"
            )
        if view.get("field_claim") == "held":
            failures.append(
                f"view {index}: the pending seat reported field_claim "
                f"held on a field that never answered a claim — "
                f"{json.dumps(view)[:200]}"
            )
        if view.get("sync") not in HONEST_SYNC:
            failures.append(
                f"view {index}: the pending seat reported a sync "
                f"verdict it cannot hold — {json.dumps(view)[:200]}"
            )
    if any(read.get("source_owns_field") is True for read in reads):
        failures.append(
            "the pending seat's served checkpoint stamped "
            "source_owns_field true — a pending run serves state but "
            "claims nothing"
        )
    ticks = integers(window.get("ticks") or [])
    if not ticks:
        failures.append(
            "the answered /role reads carried no integer tick — the "
            "pending run's cadence is unreadable"
        )
    elif ticks[-1] - ticks[0] < TICK_FLOOR:
        failures.append(
            f"the pending seat's tick advanced {ticks[-1] - ticks[0]} "
            f"across the "
            f"{format(window.get('window_s', FROZEN_WINDOW), '.0f')}s "
            f"frozen window — below the bounded degraded floor of "
            f"{TICK_FLOOR}"
        )
    for read in reads:
        bound = read.get("bound", MIRROR_BOUND)
        label = (
            f"{read.get('kind', 'mirror')} {read.get('method', 'GET')} "
            f"{read.get('path')}"
        )
        if not read.get("answered"):
            failures.append(
                f"{label} never answered inside its "
                f"{format(bound, '.1f')}s bound on the serving monitor"
                + (" — " + str(read.get("error")) if read.get("error") else "")
            )
            continue
        if (read.get("elapsed") or 0) > bound:
            failures.append(
                f"{label} answered in "
                f"{format(read['elapsed'], '.2f')}s past its "
                f"{format(bound, '.1f')}s bound — the wedged scan's lock "
                "hold leaked serialized field timeouts into the serving "
                "lane"
            )
            continue
        if read.get("status") != read.get("want_status", 200):
            failures.append(
                f"{label} answered status {read.get('status')} — the "
                f"pending contract names {read.get('want_status', 200)}"
            )
            continue
        want = read.get("want_verdict")
        if want is not None and read.get("verdict") != want:
            failures.append(
                f"{label} answered {read.get('verdict')} — the pending "
                f"contract names {want}"
            )
    probed = {
        read.get("path") for read in reads if read.get("kind") == "lock"
    }
    for _method, path, _status, _verdict in LOCK_CALLS:
        if path not in probed:
            failures.append(
                f"the frozen window never reached {path} — the pass "
                "cannot speak for its bound"
            )
    ages = integers(window.get("ages") or [])
    if ages and max(ages) > SCAN_AGE_BOUND:
        failures.append(
            f"the served last_scan_age_ms reached {max(ages)}ms past the "
            f"{SCAN_AGE_BOUND}ms bound — the paced scan is not completing "
            "near one field timeout"
        )
    # The doctored expectation: the recorded defect's serialized stall
    # demanded of every lock-taking endpoint. The honest bounded run
    # cannot satisfy it, and must say so rather than pass.
    if tamper == "expect-serialized":
        for read in reads:
            if read.get("kind") != "lock":
                continue
            elapsed = read.get("elapsed") or 0
            if elapsed < SERIALIZED_FLOOR:
                failures.append(
                    f"{TAMPER_EVIDENCE} — {read.get('method')} "
                    f"{read.get('path')} answered in "
                    f"{format(elapsed, '.2f')}s, short of the "
                    f"{format(SERIALIZED_FLOOR, '.0f')}s stall the "
                    "doctored expectation demands"
                )


def judge_recovery(recovery, failures):
    """The thaw's clause — the deferred conditional grant landed per the
    standing born-active contract, journaled under the reclaim
    origin."""
    settled = recovery.get("settled")
    if (
        not isinstance(settled, dict)
        or settled.get("role") != "active"
        or settled.get("field_claim") != "held"
    ):
        failures.append(
            "the thawed field never landed the deferred conditional "
            "grant — the pending seat never reached active with "
            f"field_claim held: {json.dumps(settled)[:200]}"
        )
    if not recovery.get("grant_journaled"):
        failures.append(
            "the deferred grant's landing never journaled the standby "
            "-> promoting -> active walk under the reclaim origin: "
            f"{recovery.get('walk')}"
        )


def serving_marks(window):
    """The window's normalized marks — the digest's record of what held,
    free of every measured latency so two passes agree."""
    reads = window.get("reads") or []
    answered = [read for read in reads if read.get("answered")]
    ticks = integers(window.get("ticks") or [])
    ages = integers(window.get("ages") or [])
    mirrors = [
        read for read in answered if read.get("kind") == "mirror"
    ]
    return {
        "lock": sorted(
            f"{read['method']} {read['path']}={read.get('verdict')}"
            for read in answered
            if read.get("kind") == "lock"
        ),
        "mirror": sorted(read["path"] for read in mirrors),
        "mirrors": "instant"
        if mirrors
        and all(
            (read.get("elapsed") or 0) <= MIRROR_BOUND for read in mirrors
        )
        else "late",
        "cadence": "bounded"
        if ticks and ticks[-1] - ticks[0] >= TICK_FLOOR
        else "defect",
        "scan_age": "bounded"
        if ages and max(ages) <= SCAN_AGE_BOUND
        else "defect",
        "surface": sorted(
            f"{view.get('role')}/{view.get('sync')}/"
            f"{view.get('field_claim')}"
            for view in window.get("views") or []
        ),
        "claims_field": any(
            read.get("source_owns_field") is True for read in reads
        ),
    }


def pair_held(rig, failures, when):
    """The deployed pair's launch roles mid-leg — the field owner
    `active`, the declared standby `tracking` it — beside one
    tracking-first pair tick, the legs' shared shape."""
    held = driver_recovery.roles_hold(rig, failures, when)
    rig.tick(rig.standby_url, rig.duty_url, failures)
    return "active+tracking" if held else "moved"


def await_role(url, want, bound):
    """The first served role report matching `want`, or None inside the
    bound — the wait the staged seat's own serving needs."""
    deadline = time.monotonic() + bound
    while time.monotonic() < deadline:
        try:
            _status, report = serve("GET", url + "/role", None, MIRROR_BOUND)
        except Exception:
            report = None
        if isinstance(report, dict) and want(report):
            return report
        time.sleep(WINDOW_POLL)
    return None


def pending_serving_bound_pass(args, tamper):
    """The pending bounded-serving run: converge the deployed pair, stage
    the frozen field and the labeled born-active on it, read the serving
    bounds across the frozen window, thaw the field and read the
    deferred grant's landing, then restore the launch set. Returns
    `(digest_entries, evidence, failures)`; raises `Inconclusive` where
    the pinned release predates the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "pending-serving-bound leg has nothing to exercise"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = None
    field = None
    seat = None
    plant = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url

        # Phase 1 — the settled deployment the staged seat runs beside:
        # the field owner `active`, the declared standby `tracking` it.
        # A duty that never reported its startup claim is a release
        # predating the substrate this leg reads through.
        converged = rig.converge(failures)
        evidence["converged"] = converged["owner"]["tick"]
        if failover.owner_token(rig.duty_preamble) is None:
            raise Inconclusive(
                "the pinned release predates the startup-claim record "
                "the deployed pair's substrate reads",
                "the field owner's startup log carries no "
                "write-ownership claim line",
            )
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty": "active",
                "standby": "tracking",
            }
        )
        schema = pair.get(f"{duty_url}/schema", "GET /schema", failures)
        declared_command = pair.declared_command(schema)
        if declared_command is None:
            raise Abort(
                "the served registry declares no command — the pending "
                "surface's not_active refusal has nothing to refuse"
            )
        component, spec = declared_command
        command = {
            "command": {
                "invoke": {
                    "component": component,
                    "command": spec["name"],
                    "arguments": simulate.command_arguments(spec),
                }
            },
            "actor": ACTOR,
        }

        # Phase 2 — the frozen field and the labeled born-active: a
        # listener that completes every handshake and answers nothing,
        # and a paced `--remote` launch standing pending on it beside
        # the settled pair.
        field = FrozenField(pair.closed_port())
        files = scratch_files(rig, "pending-seat")
        seat, seat_url, preamble = spawn_pending_or_classify(
            args, field.address_text, files
        )
        if await_role(seat_url, lambda _report: True, SERVE_SETTLE) is None:
            raise Abort(
                "the labeled born-active never served its monitor while "
                "standing on the frozen field — "
                + ("; ".join(preamble) or "no startup diagnostic")
            )
        if not any("stands pending" in line for line in preamble):
            failures.append(
                "the staged launch's startup log carries no pending "
                f"report: {preamble}"
            )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "stage",
                "pacing": f"--scan-ms {PENDING_SCAN_MS}",
                "pending": "reported",
                "pair": pair_held(
                    rig,
                    failures,
                    "while the pending seat stood on the frozen field",
                ),
            }
        )

        # Phase 3 — the frozen window: every lock-taking endpoint
        # bounded near one field timeout with its named verdict, the
        # lock-free mirrors instant, the degraded cadence and the served
        # scan age bounded, the pending surface honest.
        window = frozen_window(seat_url, command)
        predating_release = predating(window)
        if predating_release is not None:
            raise Inconclusive(*predating_release)
        judge_window(window, failures, tamper)
        digest_entries.append({"phase": "frozen", **serving_marks(window)})

        # Phase 4 — the thaw: the frozen field released, a real plant
        # bound at the same address, and the deferred conditional grant
        # re-issued on the first answered contact. The seat is stopped
        # before its durable file is read, so the walk the file carries
        # is the walk the run completed.
        address = field.address_text
        field.thaw()
        field = None
        plant = spawn_plant_at(args, address)
        recovery = {
            "settled": await_role(
                seat_url,
                lambda report: report.get("role") == "active"
                and report.get("field_claim") == "held",
                SERVE_SETTLE,
            ),
            "grant_journaled": False,
            "walk": None,
        }
        pair.stop(seat)
        seat = None
        walk = stranded_rejoin.role_walk(
            stranded_rejoin.journal_entries(files["journal_file"])
        )
        recovery["walk"] = walk
        recovery["grant_journaled"] = (
            ("standby", "promoting", "reclaim") in walk
            and ("promoting", "active", "reclaim") in walk
        )
        judge_recovery(recovery, failures)
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "thaw",
                "settled": "active",
                "claim": "held",
                "landing": "journaled-reclaim",
            }
        )

        # Phase 5 — the launch set restored: the staged seat and the
        # staged field are gone, and the deployed pair rests on its
        # launch roles for the legs behind this one.
        for _ in range(SETTLE_TICKS):
            rig.tick(standby_url, duty_url, failures)
        if not driver_recovery.roles_hold(
            rig, failures, "after the pending-serving-bound staging"
        ):
            raise Abort
        digest_entries.append(
            {
                "phase": "restore",
                "ticks": SETTLE_TICKS,
                "duty": "active",
                "standby": "tracking",
                "launch": "staged-seat-removed",
            }
        )
        evidence["final_tick"] = pair.get(
            f"{duty_url}/role", "GET /role", failures
        ).get("tick")
    except Inconclusive:
        raise
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args if str(arg))
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        if seat is not None:
            pair.stop(seat)
        if field is not None:
            field.thaw()
        if plant is not None:
            pair.stop(plant)
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
        choices=["expect-serialized"],
        help="doctor the leg's expectation to the recorded defect — "
        "demanding that every lock-taking endpoint queue the wedged "
        "scan's serialized field timeouts — so the pass must fail "
        "naming the bounded responses it observed instead",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = pending_serving_bound_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                f"pending-serving-bound: {TAMPER_EVIDENCE} — an "
                "inconclusive run offers the doctored case no evidence"
            )
            return 1
        # The digest line carries only the stable reason — the run's own
        # verdicts and endpoints report on stderr, where two identical
        # passes need not share them.
        reason = inconclusive.args[0]
        detail = inconclusive.args[-1]
        eprint(f"pending-serving-bound: inconclusive — {detail}")
        print(f"pending-serving-bound-digest inconclusive — {reason}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"pending-serving-bound: {line}")
        if args.tamper is not None:
            eprint(f"pending-serving-bound: {TAMPER_EVIDENCE}")
        return 1
    for failure in failures:
        eprint(f"pending-serving-bound: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"pending-serving-bound: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored "
                "expectation"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"pending-serving-bound-digest {digest} — the manifest-declared "
        f"pair converged at tick {evidence['converged']}, the labeled "
        "born-active stood pending on the frozen field with every "
        "lock-taking endpoint inside the declared "
        f"{format(LOCK_BOUND, '.1f')}s bound (checkpoint document, "
        "promote not_converged, demote not_active, and the receipted "
        "command not_active), the lock-free /role and /health mirrors "
        "instant, the bounded degraded cadence and the served "
        f"last_scan_age_ms inside {SCAN_AGE_BOUND}ms, the thawed field "
        "landed the deferred conditional grant under the reclaim origin "
        "with the claim held, and the launch set is restored with the "
        f"pair on its launch roles at tick {evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())