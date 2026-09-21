#!/usr/bin/env python3
"""The monitor-starvation leg for the reference plant — the
consumer-side proof that the lane-split serving contract holds on the
deployed redundant pair: an incomplete-request-body flood against the
field owner's monitor starves only the bounded submission lane while
the serving lane keeps answering (WW-ENG-003, WW-FND-004).

The consumer-boundary stage's malformed/flood schedule drives the same
shapes against the scripted simulation's lone controller, but nothing
starves the deployed pair's live monitors — a released controller whose
serving lane regressed would pass clean CI while a customer's own
stalled client re-created the spurious-failover window: every worker
pinned on a body read, the standby's checkpoint pulls missing until
its armed budget fired the promotion. This leg runs the armed pair the
manifest declares — `pair.launch_pair` spawning the released tooling
with the standby's `--auto-promote` carrying the declared
`failover_budget` — converges it to `tracking`, then holds the
saturating set of incomplete-body connections against the field
owner's monitor. Through the hold:

- `GET /role`, `GET /snapshot`, and `GET /checkpoint` keep answering
  inside the declared per-request bound on *both* peers — the reads a
  stalled-body flood must never starve;
- the standby's checkpoint pulls keep landing: each driven `POST
  /scan` on the tracking peer pulls the flooded owner's checkpoint
  through the serving lane, the served `GET /role` holding
  `standby`/`tracking` aligned at the owner's tick — the hold spans
  one more pull than the armed budget, so a starved heartbeat would
  produce the spurious self-promotion inside the window;
- the field's writer claim still fences a foreign attachment's
  mutation probe;
- neither peer's durable `--journal-file` carries a `role_changed` or
  `field_claim_lost` entry.

Closing the set frees the submission lane: a driven `POST /scan` on
the flooded owner answers again and advances the tick, a receipted
kind-declared command settles `applied` into both peers' adopted log,
and the pair's roles stand unchanged.

Usage:

    monitor_starvation.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `monitor-starvation-digest <sha256>` line prints — the
check runs two passes and compares them
(`monitor-starvation-nondeterministic`). A contract violation reports
`monitor-starvation: …` lines on stderr and exits 1 — the check's
`monitor-starvation-failed`. The doctored cases prove the leg's
assertions fire: `--tamper starved-reads` doctors the declared
per-request bound to zero so every liveness read observes starvation,
and `--tamper peer-transition` posts a promote to the tracking standby
mid-hold — the genuine role change the hold must report rather than
pass.
"""

import argparse
import hashlib
import json
import socket
import sys
import time
import urllib.request

import failover
import pair
import simulate


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

ACTOR = "ci-monitor-starvation"

# The declared per-request bound the serving lane owes — the same
# bound the rig-side starvation leg asserts. A read that cannot answer
# inside it under the hold is the starvation the lane split exists to
# prevent.
SERVE_BOUND = 2.0

# The saturating set — past both lanes' worker pools summed, the shape
# the reproduction pinned every worker with. The pinning requests: an
# Expect request's held body reader and a chunked scan's unterminated
# first chunk, each holding its submission-lane worker until the
# connection closes.
STARVE_CONNECTIONS = 8
STARVE_REQUESTS = (
    b"POST /command HTTP/1.1\r\nHost: q\r\nContent-Length: 16\r\n"
    b"Expect: 100-continue\r\n\r\n",
    b"POST /scan HTTP/1.1\r\nHost: q\r\nTransfer-Encoding: chunked\r\n"
    b"\r\n10\r\n",
)

# The serving-lane reads the hold samples on each peer.
READ_ENDPOINTS = ("/role", "/snapshot", "/checkpoint")

# The attempts one sampled read gets before its miss is named: each
# attempt is a fresh request owed an answer inside the declared bound,
# but the sample only reports a miss when every attempt misses — one
# scheduling stall on a shared runner cannot read as the pinned-lane
# regression the leg exists to catch, while a starved lane misses
# them all.
STARVE_ATTEMPTS = 3

# The transition events a starvation window must never add to either
# peer's durable journal — a role change or a lost field claim is the
# spurious-failover record.
FORBIDDEN_EVENTS = ("role_changed", "field_claim_lost")


def monitor_address(url):
    """The `(host, port)` a monitor url serves."""
    host, port = url.removeprefix("http://").rsplit(":", 1)
    return host, int(port)


def starvation_flood(url):
    """Open the saturating set of incomplete-body connections against
    the monitor at `url` — each carrying a pinning request whose
    declared body never follows. Returns the open sockets; raises
    OSError when the set never opens."""
    streams = []
    try:
        for index in range(STARVE_CONNECTIONS):
            stream = socket.create_connection(
                monitor_address(url), timeout=5
            )
            stream.sendall(STARVE_REQUESTS[index % len(STARVE_REQUESTS)])
            streams.append(stream)
    except OSError:
        for stream in streams:
            stream.close()
        raise
    return streams


def sampled_read(url, bound):
    """One bounded liveness sample: GET `url` up to STARVE_ATTEMPTS
    times, each attempt a fresh request owed an answer inside `bound`
    seconds. Returns `(body, verdict, detail)` — `bounded` with the
    decoded body when an attempt answers inside the bound, `late`
    when attempts answered but none inside it, `starved` when no
    attempt answered at all; `detail` is the last attempt's error."""
    answered = False
    error = None
    for _ in range(STARVE_ATTEMPTS):
        started = time.monotonic()
        try:
            with urllib.request.urlopen(url, timeout=bound) as response:
                body = json.load(response)
        except Exception as exc:
            error = exc
            continue
        answered = True
        if time.monotonic() - started <= bound:
            return body, "bounded", None
    return None, "late" if answered else "starved", error


def forbidden_events(path):
    """The `FORBIDDEN_EVENTS` entries a durable `--journal-file`
    carries — the transition record a starvation window must not
    produce."""
    found = []
    for kind, record in pair.journal_records(path):
        if kind != "entry":
            continue
        event = record.get("event", {})
        found.extend(name for name in FORBIDDEN_EVENTS if name in event)
    return found


def starvation_pass(args, tamper):
    """The starvation run: converge the armed pair, flood the field
    owner's monitor, hold the window, then prove the submission lane
    recovers. Returns `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "monitor-starvation leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    budget = standby_decl.get("failover_budget")
    if not isinstance(budget, int) or isinstance(budget, bool) or budget < 1:
        raise Abort(
            "the manifest's standby declares no positive "
            "failover_budget — the starvation leg's armed heartbeat "
            "has nothing to measure"
        )
    with open(args.model) as handle:
        model = json.load(handle)
    points = failover.signal_points(model)
    if points is None:
        raise Abort(
            "the emitted model declares no fencing-probe field point "
            "— the monitor-starvation leg has nothing to exercise"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = probe_io = None
    streams = []
    try:
        rig = pair.launch_pair(args, declared, auto_promote=budget)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        # The genuinely foreign attachment the fencing probes run on —
        # the run's own plant-protocol client can join a recorded
        # claim's holder set, so it never runs mutation probes.
        probe_io = simulate.PlantClient(rig.plant_addr)
        duty_token = failover.owner_token(rig.duty_preamble)
        # The `starved-reads` tamper doctors the declared bound to
        # zero — every liveness read observes starvation, the verdict
        # a regressed serving lane would produce.
        bound = 0.0 if tamper == "starved-reads" else SERVE_BOUND

        # Phase 1 — convergence on the armed pair: the standby
        # tracking with its --auto-promote budget armed, so a missed
        # heartbeat inside the hold would fire the spurious promotion
        # the lane split prevents.
        converged = rig.converge(failures)
        owner_tick = converged["owner"]["tick"]
        evidence["converged"] = owner_tick
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )
        held = failover.field_read(rig.plant_io, points["cmd"], failures)[
            "value"
        ]

        # Phase 2 — the hold: the saturating set open against the
        # field owner's monitor while each round samples the three
        # liveness reads on both peers inside the declared bound,
        # drives one standby scan whose checkpoint pull is the armed
        # heartbeat, and probes the field's writer claim. `budget + 1`
        # rounds witness more pulls than the armed miss budget — a
        # starved heartbeat self-promotes inside the window.
        streams = starvation_flood(duty_url)
        peers = (
            (duty_decl["name"], duty_url, "active"),
            (standby_decl["name"], standby_url, "standby"),
        )
        rounds = budget + 1
        reads = {
            name: {
                path: {"bounded": 0, "late": 0, "starved": 0}
                for path in READ_ENDPOINTS
            }
            for name, _url, _role in peers
        }
        pulls = {"driven": 0, "landed": 0}
        fencing = []
        for round_ in range(rounds):
            for name, url, want in peers:
                for path in READ_ENDPOINTS:
                    body, verdict, detail = sampled_read(url + path, bound)
                    reads[name][path][verdict] += 1
                    if verdict == "starved":
                        failures.append(
                            f"GET {path} on {name} never answered "
                            f"inside the declared {bound}s bound under "
                            f"the hold: {detail}"
                        )
                        continue
                    if verdict == "late":
                        failures.append(
                            f"GET {path} on {name} answered past the "
                            f"declared {bound}s bound on every attempt "
                            "under the hold"
                        )
                        continue
                    if path == "/role":
                        role = (
                            body.get("role") if isinstance(body, dict) else None
                        )
                        if role != want:
                            failures.append(
                                f"{name} reports role {role!r} under "
                                f"the hold, expected {want} — the "
                                "transition the lane split exists to "
                                "prevent"
                            )
            # The doctored role change: a promote posted to the
            # tracking standby mid-hold — the genuine transition the
            # leg's audits must catch.
            if tamper == "peer-transition" and round_ == 0:
                pair.request(f"{standby_url}/promote", {})
            # The armed heartbeat: one driven scan on the tracking
            # peer pulls the flooded owner's checkpoint through the
            # serving lane. A starved pull reports degraded; `budget`
            # of them self-promotes.
            pair.scan(standby_url, failures)
            pulls["driven"] += 1
            report = pair.get(f"{standby_url}/role", "GET /role", failures)
            sync = report.get("sync")
            if report.get("role") != "standby":
                failures.append(
                    f"the armed standby moved to role "
                    f"{report.get('role')!r} under the hold — the "
                    "spurious failover the lane split exists to prevent"
                )
            elif not (isinstance(sync, dict) and "tracking" in sync):
                failures.append(
                    "the standby's checkpoint pull stopped landing "
                    f"under the hold — GET /role reports sync {sync}"
                )
            else:
                aligned = (sync.get("tracking") or {}).get("aligned")
                if aligned != owner_tick:
                    failures.append(
                        f"the standby's checkpoint pull aligned at "
                        f"{aligned} while the field owner's checkpoint "
                        f"stands at {owner_tick} — the pull did not land"
                    )
                else:
                    pulls["landed"] += 1
            probe = failover.foreign_probe(probe_io, points["cmd"], held)
            kind = failover.probe_kind(probe)
            fencing.append(kind)
            want_claim = "fenced" if duty_token is not None else "granted"
            if kind != want_claim:
                failures.append(
                    f"a foreign attachment's field probe answered "
                    f"{kind} under the hold — expected the writer "
                    f"claim {want_claim}"
                )
        if not fencing:
            failures.append(
                "the field never answered a fencing probe under the hold"
            )
        # The durable-journal audit — the transition record the hold
        # must not produce on either peer.
        journal_clean = True
        for name, files in (
            (duty_decl["name"], rig.duty_files),
            (standby_decl["name"], rig.standby_files),
        ):
            journal_path = files.get("journal_file")
            if journal_path is None:
                continue
            found = forbidden_events(journal_path)
            if found:
                journal_clean = False
                failures.append(
                    f"{name}'s durable journal carries {sorted(set(found))} "
                    "through the hold — the transition record the "
                    "starvation window must not produce"
                )
        digest_entries.append(
            {
                "phase": "hold",
                "connections": STARVE_CONNECTIONS,
                "rounds": rounds,
                "owner_tick": owner_tick,
                "reads": {
                    name: {
                        path: (
                            "bounded"
                            if entry["bounded"] == rounds
                            else "starved"
                            if entry["starved"] == rounds
                            else "late"
                        )
                        for path, entry in endpoints.items()
                    }
                    for name, endpoints in reads.items()
                },
                "pulls": pulls,
                "fencing": fencing,
                "journal": "clean" if journal_clean else "transitioned",
            }
        )
        for stream in streams:
            stream.close()
        streams = []
        if failures:
            raise Abort

        # Phase 3 — recovery: closing the set frees the flooded
        # submission lane. A driven `POST /scan` on the field owner
        # answers again and advances the tick, the tracking peer's
        # next pull adopts it back, and a receipted kind-declared
        # command settles `applied` into both peers' adopted log.
        resumed = pair.scan(duty_url, failures)
        if resumed.get("tick") != owner_tick + 1:
            failures.append(
                f"the field owner's first post-hold scan landed at "
                f"tick {resumed.get('tick')}, expected {owner_tick + 1} "
                "— the driven scan path did not resume"
            )
            raise Abort
        evidence["resumed"] = resumed["tick"]
        _tracked, owner = rig.tick(standby_url, duty_url, failures)
        schema = pair.get(f"{duty_url}/schema", "GET /schema", failures)
        command_spec = pair.declared_command(schema)
        if command_spec is None:
            failures.append(
                "the served registry declares no command — the "
                "recovery leg has nothing to submit"
            )
            raise Abort
        component, spec = command_spec
        command = {
            "invoke": {
                "component": component,
                "command": spec["name"],
                "arguments": simulate.command_arguments(spec),
            }
        }
        status, receipt = pair.request(
            f"{duty_url}/command", {"command": command, "actor": ACTOR}
        )
        if status != 200 or simulate.receipt_outcome(receipt) != "accepted":
            failures.append(
                f"the recovery command answered {status} {receipt} — "
                "the submission lane never freed"
            )
            raise Abort
        _tracked, owner = pair.tick(standby_url, duty_url, failures)
        for _ in range(5):
            standby_role = pair.get(
                f"{standby_url}/role", "GET /role", failures
            )
            sync = standby_role.get("sync")
            if isinstance(sync, dict) and "tracking" in sync:
                break
            pair.scan(duty_url, failures)
            _tracked, owner = pair.tick(standby_url, duty_url, failures)
        else:
            failures.append(
                "the tracking peer never reconverged after the "
                "recovery command's settling tick"
            )
            raise Abort
        receipts_duty = pair.get(
            f"{duty_url}/receipts", "GET /receipts", failures
        )
        receipts_standby = pair.get(
            f"{standby_url}/receipts", "GET /receipts", failures
        )
        if receipts_duty != receipts_standby:
            failures.append(
                "the peers' receipt logs diverged across the recovery "
                "— the adopted audit is not one log"
            )
            raise Abort
        if not any(
            entry.get("command") == command
            and simulate.receipt_outcome(entry) == "applied"
            for entry in receipts_duty
        ):
            failures.append(
                "the recovery command never settled applied into the "
                "adopted receipt log"
            )
            raise Abort
        evidence["settled"] = owner["tick"]
        # The pair's roles stand unchanged and neither durable journal
        # carries a transition.
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        if duty_role.get("role") != "active":
            failures.append(
                f"the field owner reports {duty_role.get('role')!r} "
                "after the hold, expected active"
            )
        sync = standby_role.get("sync")
        if standby_role.get("role") != "standby" or not (
            isinstance(sync, dict) and "tracking" in sync
        ):
            failures.append(
                "the tracking peer reports "
                f"{standby_role.get('role')!r}/{sync} after the hold, "
                "expected standby/tracking"
            )
        for name, files in (
            (duty_decl["name"], rig.duty_files),
            (standby_decl["name"], rig.standby_files),
        ):
            journal_path = files.get("journal_file")
            if journal_path is not None and forbidden_events(journal_path):
                failures.append(
                    f"{name}'s durable journal carries a transition "
                    "the starvation window must not produce"
                )
        digest_entries.append(
            {
                "phase": "recovery",
                "resumed": resumed["tick"],
                "command": {"component": component, "name": spec["name"]},
                "receipt": receipt,
                "receipts": receipts_duty,
                "duty_role": duty_role,
                "standby_role": standby_role,
            }
        )
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        for stream in streams:
            stream.close()
        if probe_io is not None:
            probe_io.close()
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
        choices=["starved-reads", "peer-transition"],
        help="doctor the leg — the pass must fail naming the evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = starvation_pass(
            args, args.tamper
        )
    except Abort as abort:
        for line in abort.args:
            eprint(f"monitor-starvation: {line}")
        return 1
    for failure in failures:
        eprint(f"monitor-starvation: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"monitor-starvation: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored run"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    pulls = digest_entries[1]["pulls"]
    print(
        f"monitor-starvation-digest {digest} — serving lane bounded "
        f"under {STARVE_CONNECTIONS} incomplete-body connections "
        f"through {pulls['landed']} witnessed checkpoint pulls at "
        f"tick {evidence['converged']}, fencing probes fenced, "
        f"recovery command applied by tick {evidence['settled']}, "
        "roles unchanged"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
