#!/usr/bin/env python3
"""The consumer-boundary driver for the reference plant (WW-FND-004).

Runs the same deterministic `--driven` scenario `ci/simulate.py`
performs — `dcs-plant-server` serving the checked-in model and
dynamics, `dcs-controller --driven` advancing scans on the driver's
`POST /scan` requests — but under one named consumer schedule overlaid
on the monitor's documented endpoints:

- `zero-clients` — no consumer attached; the control run every other
  schedule's digest is compared against;
- `polling` — a consumer polling every read surface in a loop;
- `stalled-reader` — a reader that issues `GET /snapshot` and holds the
  connection open without reading a byte of the response until the run
  ends;
- `disconnect-reconnect` — connect, request, read some or none of the
  response, drop — repeated, sometimes mid-response;
- `malformed-and-flood` — garbage bytes, half-sent requests, refused
  verbs, and malformed bodies within the declared limits, beside a hot
  read loop over every surface;
- `ui-restart` — a separate UI consumer process (this script's
  `--ui-client` mode, polling with `since` cursors the way the served
  page does) killed at the run's midpoint and restarted, rejoining on
  the served freshness metadata.

The driven monitor's `POST /scan` is the run's drive channel — the
driver owns it, so no schedule issues a valid scan request. The paced
deployment's refusal of endpoint-driven scans is the runtime-side
matrix's case (`dcs-monitor`'s non-interference suite); here the
declared limits the flood exercises are that malformed input is
refused at parse, refused verbs get their named status, and reads are
served out of the bounded publication store.

Every schedule runs the identical script — the scenario's legs, their
receipted commands, their asserted points — so one digest over the leg
outcomes and command receipts must come out identical under every
schedule and across repeated runs. A run exits 1 naming the schedule
and the failed evidence on stderr; `ci/check.sh` reports that as
`consumer-interference`, or `consumer-nondeterministic` when two stage
passes differ.

Usage:

    consumers.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --schedule <name>

The `--ui-client` mode is the consumer half of the `ui-restart`
schedule — a standalone process the driver spawns, kills, and
respawns:

    consumers.py --ui-client --monitor http://ADDR --seen PATH --point N

It polls `GET /snapshot`, `GET /journal?since=`, and
`GET /history?point=&since=` like the served page, carries its cursors
forward, and records each poll's observation — the publication
counters, its stream positions, any detected gap, and any fault — as
the JSON document at `--seen`, so the driver can read what the
consumer saw before and after its restart.
"""

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

import simulate


def eprint(*args):
    print(*args, file=sys.stderr)


SCHEDULES = [
    "zero-clients",
    "polling",
    "stalled-reader",
    "disconnect-reconnect",
    "malformed-and-flood",
    "ui-restart",
]


class ConsumerLog:
    """What the consumers observed — the evidence that a schedule's
    interference was real rather than vacuous."""

    def __init__(self):
        self.lock = threading.Lock()
        # Every HTTP status a consumer read back.
        self.statuses = []
        # Transport failures — a connection refused or dropped before an
        # answer. Never expected outside a monitor outage, which these
        # schedules do not cause: the run's monitor stays up throughout.
        self.errors = []
        # Raw probes pushed at the socket level — garbage bytes and
        # half-sent requests that never became a request.
        self.probes = 0
        # The stalled reader's held response, read back at run end:
        # (status, body).
        self.stalled = None

    def record(self, result):
        """`result` is a status code or an exception from a request."""
        with self.lock:
            if isinstance(result, int):
                self.statuses.append(result)
            else:
                self.errors.append(str(result))


class Context:
    """The consumer threads' view of the run: the monitor's addresses,
    the read surfaces to cycle, the stop signal, first-contact
    accounting, and the shared observation log."""

    def __init__(self, monitor, point, log):
        self.monitor = monitor
        host, port = monitor.rsplit(":", 1)
        self.address = (host.removeprefix("http://"), int(port))
        self.read_surfaces = [
            "/snapshot",
            "/receipts",
            f"/history?point={point}&since=0",
            "/journal?since=0",
            "/checkpoint",
            "/role",
            "/signals",
            "/",
        ]
        self.stop = threading.Event()
        self.log = log
        self._contacted = 0
        self._contact_lock = threading.Lock()

    def contact(self):
        """Reports this consumer's first contact with the monitor —
        the driver waits for every expected consumer before the script
        starts, so the interference provably overlapped the run."""
        with self._contact_lock:
            self._contacted += 1

    @property
    def contacted(self):
        with self._contact_lock:
            return self._contacted


def request(ctx, method, path, body=None):
    """One HTTP request; returns the status code. Raises on transport
    failure — `ConsumerLog.record` files it as an error."""
    data = body.encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(
        ctx.monitor + path, data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            response.read()
            return response.status
    except urllib.error.HTTPError as error:
        error.read()
        return error.code


def polling(ctx):
    """A normally polling reader: one fresh request per read surface,
    round-robin, until the run ends."""
    index = 0
    contacted = False
    while not ctx.stop.is_set():
        try:
            ctx.log.record(request(ctx, "GET", ctx.read_surfaces[index]))
        except Exception as error:
            ctx.log.record(error)
        index = (index + 1) % len(ctx.read_surfaces)
        if not contacted:
            contacted = True
            ctx.contact()


def stalled(ctx):
    """A reader that issues `GET /snapshot` and then holds the
    connection without reading a byte of the response until the run
    ends — the held-response case the publication split exists for.
    Contact is reported once the request is issued, so the held
    connection provably spans the whole script."""
    try:
        stream = socket.create_connection(ctx.address, timeout=5)
    except OSError as error:
        ctx.log.record(error)
        ctx.contact()
        return
    stream.sendall(
        b"GET /snapshot HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
    )
    ctx.contact()
    while not ctx.stop.is_set():
        time.sleep(0.005)
    # The answer waited on the wire the whole time: complete, correct,
    # and never holding the scan loop.
    stream.settimeout(5)
    try:
        chunks = []
        while True:
            chunk = stream.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        text = b"".join(chunks).decode(errors="replace")
        status = int(text.split(None, 2)[1]) if text.startswith("HTTP") else 0
        ctx.log.stalled = (status, text)
    except (OSError, ValueError, IndexError) as error:
        ctx.log.record(error)
    finally:
        stream.close()


def churn(ctx):
    """Disconnect/reconnect churn: each cycle opens a connection,
    issues a read, reads the response's first bytes — or none — and
    drops, sometimes mid-response."""
    index = 0
    contacted = False
    while not ctx.stop.is_set():
        if not contacted:
            contacted = True
            ctx.contact()
        path = ctx.read_surfaces[index]
        index = (index + 1) % len(ctx.read_surfaces)
        try:
            stream = socket.create_connection(ctx.address, timeout=5)
        except OSError as error:
            ctx.log.record(error)
            continue
        try:
            stream.sendall(
                f"GET {path} HTTP/1.1\r\nHost: x\r\n"
                f"Connection: close\r\n\r\n".encode()
            )
            stream.settimeout(0.25)
            try:
                head = stream.recv(512)
            except OSError:
                head = b""
            if head.startswith(b"HTTP"):
                try:
                    ctx.log.record(int(head.split(None, 2)[1]))
                except (ValueError, IndexError):
                    pass
            # Drop without draining — the disconnect mid-response.
        finally:
            stream.close()


def flood(ctx):
    """Malformed traffic and read flooding within the declared bounds:
    garbage bytes, unparsable bodies, bad queries, refused verbs — none
    of it may reach authoritative state — interleaved with a hot read
    loop over every surface. No well-formed mutation appears here: a
    receipted command is a run input, not interference, and the driven
    monitor's `POST /scan` is the driver's channel — only its malformed
    forms appear, each refused at parse. `POST /promote` on the settled
    active is the named `already_active` refusal."""
    malformed = [
        ("GET", "/nonexistent", None),
        ("POST", "/snapshot", None),
        ("DELETE", "/receipts", None),
        ("PUT", "/scan", None),
        ("GET", "/history?point=abc", None),
        ("GET", "/history?since=-1", None),
        ("GET", "/journal?since=soon", None),
        ("POST", "/command", "{"),
        ("POST", "/command", '{"command":{"bogus":1}}'),
        ("POST", "/command", '{"actor":3}'),
        (
            "POST",
            "/command",
            '{"write_value":{"point":10,"kind":"float","value":"high"}}',
        ),
        ("POST", "/scan", "{"),
        ("POST", "/scan", '{"scans":-1}'),
        ("POST", "/promote", None),
    ]
    contacted = False
    while not ctx.stop.is_set():
        for path in ctx.read_surfaces:
            try:
                ctx.log.record(request(ctx, "GET", path))
            except Exception as error:
                ctx.log.record(error)
        for method, path, body in malformed:
            try:
                ctx.log.record(request(ctx, method, path, body))
            except Exception as error:
                ctx.log.record(error)
        # Raw garbage on the socket — never a request at all.
        try:
            stream = socket.create_connection(ctx.address, timeout=5)
            stream.sendall(b"\x89not-an-http-request\x90\r\n\r\n")
            stream.shutdown(socket.SHUT_WR)
            stream.settimeout(0.25)
            try:
                stream.recv(4096)
            except OSError:
                pass
            stream.close()
            ctx.log.probes += 1
        except OSError as error:
            ctx.log.record(error)
        # A request abandoned half-sent.
        try:
            stream = socket.create_connection(ctx.address, timeout=5)
            stream.sendall(b"GET /snapshot HTT")
            stream.shutdown(socket.SHUT_RDWR)
            stream.close()
            ctx.log.probes += 1
        except OSError as error:
            ctx.log.record(error)
        if not contacted:
            contacted = True
            ctx.contact()


# (behavior, threads) per schedule — the thread count the driver waits
# to see make contact before the script starts. `ui-restart` is a
# managed subprocess, not a thread behavior.
THREADS = {
    "zero-clients": [],
    "polling": [(polling, 1)],
    "stalled-reader": [(stalled, 1)],
    "disconnect-reconnect": [(churn, 2)],
    "malformed-and-flood": [(flood, 2)],
}


def read_seen(path):
    """The UI consumer's last recorded observation, or None while it
    has not written one."""
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def wait_seen(path, polls=1, deadline=10.0):
    """Waits until the seen file holds an observation with at least
    `polls` completed polls — a spawned UI process's first-contact
    signal."""
    deadline = time.monotonic() + deadline
    while time.monotonic() < deadline:
        seen = read_seen(path)
        if seen is not None and seen.get("polls", 0) >= polls:
            return seen
        time.sleep(0.02)
    return None


def ui_client(args):
    """The `--ui-client` process: a disposable UI consumer polling the
    monitor like the served page — `GET /signals` once, then snapshot,
    journal, and history on `since` cursors — and recording each
    observation at `--seen` so the driver can compare what it saw
    before and after a restart. Faults are counted, never fatal to the
    run: a monitoring outage is consumer health, not plant state."""
    seen_tmp = args.seen + ".tmp"

    def record(observation):
        with open(seen_tmp, "w") as handle:
            json.dump(observation, handle)
        os.replace(seen_tmp, args.seen)

    faults = polls = 0
    journal_gap = False
    journal_since = history_since = 0
    while True:
        try:
            if polls == 0:
                simulate.http(f"{args.monitor}/signals")
            snapshot = simulate.http(f"{args.monitor}/snapshot")
            journal = simulate.http(
                f"{args.monitor}/journal?since={journal_since}"
            )
            history = simulate.http(
                f"{args.monitor}/history?point={args.point}&since={history_since}"
            )
        except urllib.error.HTTPError as error:
            if error.code >= 500:
                faults += 1
            time.sleep(0.05)
            continue
        except Exception:
            faults += 1
            time.sleep(0.05)
            continue
        polls += 1
        if journal:
            if journal_since and journal[0]["seq"] > journal_since + 1:
                # The cursor's successors were evicted: the numbering
                # gap is the signal to coalesce onto the retained tail.
                journal_gap = True
            journal_since = journal[-1]["seq"]
        for entry in history:
            if entry["samples"]:
                history_since = entry["samples"][-1]["seq"]
        publication = snapshot.get("publication") or {}
        record(
            {
                "polls": polls,
                "faults": faults,
                "tick": snapshot["tick"],
                "published": publication.get("published"),
                "coalesced": publication.get("coalesced"),
                "journal_seq": journal_since,
                "history_seq": history_since,
                "journal_gap": journal_gap,
            }
        )


def ui_evidence_failures(before, after):
    """The `ui-restart` schedule's evidence: both incarnations polled,
    neither met a server fault, and the restarted process read the
    freshness metadata — the publication counters had moved past what
    the first saw, the named-coalescing record of the stretch it
    missed."""
    failures = []
    if before is None:
        failures.append("the UI process never polled before its restart")
    if after is None:
        failures.append("the restarted UI process never rejoined")
    if before is None or after is None:
        return failures
    if after.get("faults", 0) or before.get("faults", 0):
        failures.append(
            f"the UI process met faults: {before.get('faults')} before, "
            f"{after.get('faults')} after the restart"
        )
    published_before = before.get("published")
    published_after = after.get("published")
    if published_before is None or published_after is None:
        failures.append("the served snapshot carried no publication counters")
    elif published_after <= published_before:
        failures.append(
            "the restarted UI saw no freshness advance "
            f"({published_before} -> {published_after})"
        )
    if not after.get("coalesced"):
        failures.append(
            "the missed stretch never reported coalescing — "
            "the bounded window's named gap"
        )
    return failures


def log_evidence_failures(schedule, log, contacted, expected):
    """The thread-schedules' evidence: the interference provably
    happened, saw the right surface, and never met a server fault."""
    failures = []
    if contacted < expected:
        failures.append(
            f"only {contacted} of {expected} consumers made contact"
        )
    if schedule == "stalled-reader":
        if log.stalled is None:
            failures.append("the stalled reader's held response never arrived")
        else:
            status, body = log.stalled
            if status != 200:
                failures.append(f"the held response answered {status}")
            elif '"tick"' not in body:
                failures.append("the held response was not a complete snapshot")
    if schedule == "malformed-and-flood":
        if not log.probes:
            failures.append("no raw probes reached the socket")
        if not any(400 <= status < 500 for status in log.statuses):
            failures.append("malformed traffic was never refused with a 4xx")
    if schedule in ("polling", "disconnect-reconnect") and not log.statuses:
        failures.append("the consumers never ran")
    if any(status >= 500 for status in log.statuses):
        failures.append(
            f"a consumer saw a server fault: {sorted(set(log.statuses))}"
        )
    if log.errors:
        failures.append(f"consumer transport errors: {log.errors[:3]}")
    return failures


def run_schedule(args, scenario, point):
    """Runs the scenario once under `--schedule`'s consumer behavior;
    returns the digest entries plus the schedule's evidence failures."""
    threads = []
    ui_process = None
    with simulate.driven_rig(
        args.plant_server,
        args.controller,
        args.model,
        args.dynamics,
        scenario["dt"],
    ) as (plant_addr, monitor):
        log = ConsumerLog()
        ctx = Context(monitor, point, log)
        for behavior, count in THREADS.get(args.schedule, []):
            for _ in range(count):
                thread = threading.Thread(target=behavior, args=(ctx,), daemon=True)
                thread.start()
                threads.append(thread)
        expected = sum(count for _, count in THREADS.get(args.schedule, []))
        contact_deadline = time.monotonic() + 10
        while ctx.contacted < expected:
            if time.monotonic() > contact_deadline:
                eprint(f"consumer: {args.schedule}: consumers never made contact")
                sys.exit(1)
            time.sleep(0.005)

        seen_path = None
        scratch = None
        before = None
        after = None
        restart_at = len(scenario["legs"]) // 2
        if args.schedule == "ui-restart":
            scratch = tempfile.mkdtemp(prefix="dcs-ui-")
            seen_path = os.path.join(scratch, "seen.json")
            ui_process = spawn_ui_client(monitor, seen_path, point)
            if wait_seen(seen_path) is None:
                ui_process.terminate()
                eprint(
                    f"consumer: {args.schedule}: "
                    "the UI process never made contact"
                )
                sys.exit(1)

        def midpoint(_index):
            nonlocal before, ui_process
            if _index == restart_at and ui_process is not None:
                ui_process.terminate()
                ui_process.wait(timeout=10)
                before = read_seen(seen_path)
                os.unlink(seen_path)
                ui_process = spawn_ui_client(monitor, seen_path, point)
                wait_seen(seen_path)

        plant_client = simulate.PlantClient(plant_addr)
        try:
            digest_entries, failures = simulate.run_legs(
                monitor,
                plant_client,
                scenario["legs"],
                between_legs=midpoint if args.schedule == "ui-restart" else None,
            )
        finally:
            ctx.stop.set()
            if ui_process is not None:
                ui_process.terminate()
                ui_process.wait(timeout=10)
            for thread in threads:
                thread.join(timeout=10)
            if seen_path is not None:
                after = read_seen(seen_path)
            if scratch is not None:
                shutil.rmtree(scratch, ignore_errors=True)

        if args.schedule == "ui-restart":
            failures += ui_evidence_failures(before, after)
        else:
            failures += log_evidence_failures(
                args.schedule, ctx.log, ctx.contacted, expected
            )
        return digest_entries, failures


def spawn_ui_client(monitor, seen_path, point):
    """Spawns this script's `--ui-client` mode as the disposable UI
    consumer process the `ui-restart` schedule kills and respawns."""
    return subprocess.Popen(
        [
            sys.executable,
            os.path.abspath(__file__),
            "--ui-client",
            "--monitor",
            monitor,
            "--seen",
            seen_path,
            "--point",
            str(point),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plant-server")
    parser.add_argument("--controller")
    parser.add_argument("--model")
    parser.add_argument("--dynamics")
    parser.add_argument("--scenario")
    parser.add_argument("--schedule", choices=SCHEDULES)
    parser.add_argument("--ui-client", action="store_true")
    parser.add_argument("--monitor")
    parser.add_argument("--seen")
    parser.add_argument("--point", type=int)
    args = parser.parse_args()

    if args.ui_client:
        ui_client(args)
        return 0

    for required in ("plant_server", "controller", "model", "dynamics", "scenario", "schedule"):
        if getattr(args, required) is None:
            parser.error(f"--{required.replace('_', '-')} is required")

    with open(args.scenario) as handle:
        scenario = json.load(handle)
    with open(args.model) as handle:
        point = json.load(handle)["io_points"][0]["id"]

    digest_entries, failures = run_schedule(args, scenario, point)
    if failures:
        for failure in failures:
            eprint(f"consumer: {args.schedule}: {failure}")
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(f"consumer-digest {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
