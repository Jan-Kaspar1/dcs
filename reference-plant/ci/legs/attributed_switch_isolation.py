#!/usr/bin/env python3
"""The attributed-switch-isolation leg for the reference plant — the
consumer-side proof that an *actor-attributed* role switch keeps the
monitor's control-lane incident-time guarantee while the submission
lane is pinned by request bodies a client never finishes sending:
`POST /demote` and `POST /promote` carrying `{"actor": "qa-…"}` answer
inside the declared bound, with the switch's own verdict, while the
pair's monitors are otherwise congested (WW-ENG-003, WW-LCM-001 — the
contract #1264's fix records, mirrored at the customer boundary beside
the rig's monitor-starvation leg).

The monitor routes each request to one of five worker pools. The
control lane serves the pair's actuation — `POST /promote` and
`POST /demote` — on workers the bulk reads can never reach, so an
operator's switchover, issued during an incident exactly when consoles
wedge, never queues behind a client that stalled mid-body. Routing
used to quarantine *every* request still carrying a body on the
submission lane, so the operator's declared-identity form — the
`{"actor": …}` envelope the durable `role_changed` records are named
by, and the form a site's own tooling sends so its audit trail names
who asked — inherited that quarantine and queued behind whatever
pinned it, on the pair whose operator switch path is where the
guarantee matters most. The attributed body is tens of bytes and
always inside the server's eager-read bound, so it arrives already
buffered: it rides the control lane beside the bare request, and only
a switch request that can still wait on the client for its body keeps
the submission lane's quarantine.

Nothing starves the deployed pair's live monitors today: the
monitor-starvation leg (`ci/legs/monitor_starvation.py`) floods the
field owner with *incomplete bodies* and proves the serving lane keeps
answering, and the workspace's own monitor tests reproduce the switch
against a wedged tracking source. Neither holds the submission lane's
pins open while an operator's attributed switch is in flight — the
case a customer deployment meets when its own monitoring stack stalls
mid-request during a failover. The run:

- converges the manifest-declared pair on the released tooling — the
  field owner `active`, the declared standby settled `tracking` it;
- records the settled baseline: each peer's `GET /health` and
  `GET /role`, and the *bodiless* control requests' own verdicts — the
  bare `POST /promote` on the field owner refused `already_active`,
  the bare `POST /demote` on the tracking standby refused
  `not_active`. The bare shape is the control lane's pre-attribution
  form and routes identically on every release, so it is the leg's
  control witness: a monitor answering the bare switch while the
  attributed one does not is quarantining bodies, the pre-contract
  routing;
- stages the congestion on *both* peers' monitors — the field owner's
  the demote targets, the tracking standby's the promote does — with
  the saturating set of connections whose declared bodies never
  arrive: severed `POST /scan` batches, one chunked with its batch body
  never following and one declaring a length no batch carries, a few
  bytes sent and the client gone mid-body. Each pins a submission
  worker for as long as its connection stays open, so the whole window
  runs with the submission lane held;
- proves the pins are real while they stand: a driven `POST /scan` on
  the congested standby — a zero-scan batch, so the probe's own late
  landing runs nothing — must *not* answer inside its witness window.
  A submission lane that answered here is a lane nothing pinned, and
  the leg has no isolation to assert;
- issues the incident-time actuation under the congestion: the
  bodiless control requests first, as the witness beside them, then
  the attributed `POST /demote` on the field owner and the attributed
  `POST /promote` on the tracking standby, each owed an answer inside
  the declared bound and each carrying the switch's own verdict
  (`demoting`, `promoting`). Each peer's `GET /health` and `GET /role`
  answer at its baseline role beside them;
- reads the durable record the attributed switch leaves: both peers'
  declared `--journal-file` gained `role_changed` entries carrying
  `origin: "request"` beside the declared actor, so the answered
  switch was the attributed form and not the bare one silently
  dropping its body;
- releases the sockets and restores the pair: tracking-first driven
  ticks settle the switch under the released lane (the promoted peer
  `active`, the demoted peer reconverged `tracking`), an attributed
  demote/promote pair runs back to the launch roles, and both peers
  report the roles the legs behind this one find.

The contract postdates the pinned release line. An attributed switch
that never answers inside the declared bound while the *bodiless*
control request on the same monitor does is the pre-contract routing —
the pinned release predates the control lane's attributed-body
guarantee — and the run reports
`attributed-switch-isolation-digest inconclusive` rather than
asserting until the manifest repins a release carrying the contract;
every released artifact set predates it until the fix lands and a
release carries it. A control lane answering *neither* shape is not
the submission lane's isolation at all, and is a failure.

Usage:

    attributed_switch_isolation.py --plant-server PATH \
        --controller PATH --model model/plant.json \
        --dynamics model/dynamics.json --scenario ci/scenario.json \
        --manifest deploy/manifest.json

On success one `attributed-switch-isolation-digest <sha256>` line
prints — the check runs two passes and compares them
(`attributed-switch-isolation-nondeterministic`). A contract violation
reports `attributed-switch-isolation: …` lines on stderr and exits 1 —
the check's `attributed-switch-isolation-failed`. `--tamper
starved-switch` doctors the declared answer bound to zero so every
control-lane sample under the staged congestion observes the
starvation the isolation exists to prevent — the attributed switch
then times out behind the congestion the leg staged, and the leg must
report the named diagnostic rather than pass an unexercised contract.
"""

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import driver_recovery
import pair
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its two
# digest-identical passes, and exercise its doctored case. The leg's
# stem names its own `attributed-switch-isolation-failed` and
# `attributed-switch-isolation-nondeterministic` diagnostics.
# The doctored case: a leg whose declared bound is cut to zero, so
# every control-lane sample under the staged congestion starves, must
# surface the named diagnostic on the honest run — never a silently
# unexercised bound.
LEG = {
    # The next free slot after the pair legs origin/main added
    # (deferred-startup-refusal 720, claim-skew-bound 730,
    # pending-source-pull 740, demote-release-stays-released 750,
    # self-standby-refusal 760, reclaim-convergence-gate 770,
    # pending-serving-bound 780, shared-state-file-refusal 790,
    # usurped-claim-reclaim 800) — the stage runs the legs in this
    # order and no two may share one. Slots are allocated in landing
    # order, so a leg concurrent with main's takes the next slot after
    # the one main's own leg already holds rather than the slot both
    # picked off the same pre-merge tree: this leg and
    # `usurped_claim_reclaim.py` each read 790 as the highest and both
    # claimed 800, which `ci/legs.py`'s discovery refuses by name —
    # `pair-legs-invalid`.
    "order": 810,
    "title": "the attributed-switch-isolation leg",
    "passes": "attributed-switch-isolation-leg",
    "tampers": [
        {
            "name": "starved-switch",
            "passed": "a starved-switch case passed the attributed-switch-isolation leg",
            "missed": "the starved-switch case did not report its named diagnostic",
            "evidence": ["never answered inside the declared"],
        },
    ],
}


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The pinned release predates the attributed-switch isolation this
    leg exercises: carried as `(reason, detail)` — `reason` the stable
    phrase the inconclusive digest line prints (two passes must share
    it), `detail` the run's own verdicts, reported on stderr only. A
    monitor still quarantining every body-carrying request — the
    attributed switch queueing behind the staged pins while the bare
    switch answers — is that release's pre-contract routing, never a
    product failure of a release that simply predates the contract."""


# The declared actor every attributed request in this leg carries — the
# `qa-*` attestation identity the durable `role_changed` records must
# name, and the witness that the answered switch was the attributed
# form rather than the bare one silently dropping its body.
ACTOR = "qa-attributed-isolation"

# The saturating set staged against each congested monitor — past the
# submission lane's worker count, so every worker is pinned for as long
# as the connections stay open. The pinning requests: a severed scan
# batch (its chunk header on the wire, the batch's body never
# following) and a batch declaring a length no batch carries, a few
# bytes sent and the client gone mid-body.
CONGESTION_CONNECTIONS = 6
CONGESTION_REQUESTS = (
    b"POST /scan HTTP/1.1\r\nHost: q\r\nTransfer-Encoding: chunked\r\n"
    b"\r\n10\r\n",
    b"POST /scan HTTP/1.1\r\nHost: q\r\n"
    b"Content-Type: application/json\r\nContent-Length: 2048\r\n\r\n"
    b'{"scan',
)
CONGESTION_SHAPES = ("severed-chunked-scan", "severed-declared-scan")

# The declared per-request bound the control lane owes an operator's
# incident-time switch while the submission lane stands pinned — the
# same bound the rig-side starvation leg asserts. A switch that cannot
# answer inside it under the congestion is the isolation the routing
# split exists to provide.
SWITCH_BOUND_S = 2.0

# The liveness reads' own bound — `GET /health` and `GET /role` ride
# the heartbeat lane, which the staged congestion never reaches.
LIVENESS_BOUND_S = 2.0

# The pin witness's window: a driven `POST /scan` answering inside it
# while the pinning set stands open was served by a lane nothing held.
PIN_BOUND_S = 0.75

# The attempts one liveness sample gets before its miss is named: each
# attempt is a fresh request owed an answer inside the declared bound,
# so one scheduling stall on a shared runner cannot read as the
# regression the leg exists to catch. A switch request is *not* retried
# — a second demote would answer a different verdict.
LIVENESS_ATTEMPTS = 2

# The driven ticks the settle and the restore each run — enough for the
# demoted peer's per-scan pull to adopt the promoted owner's image and
# for the launch roles to be re-seated.
HANDOVER_TICKS = pair.HANDOVER_TICKS
RESTORE_TICKS = 4

# The doctored bound's stable evidence — every failure the tampered
# pass records carries it, so the check's negative case finds it
# whether the honest run held the contract or a predating release
# offered the case no evidence at all.
TAMPER_EVIDENCE = "never answered inside the declared"


def monitor_address(url):
    """The `(host, port)` the monitor at `url` serves."""
    host, port = url.removeprefix("http://").rsplit(":", 1)
    return host, int(port)


def stage_congestion(url):
    """Open the saturating set of severed-body connections against the
    monitor at `url` — each carrying a pinning request whose declared
    body never follows, so a submission worker is held for as long as
    the connection stays open. Returns the open sockets; releases the
    set and raises OSError when it never opens."""
    streams = []
    try:
        for index in range(CONGESTION_CONNECTIONS):
            stream = socket.create_connection(
                monitor_address(url), timeout=5
            )
            stream.sendall(
                CONGESTION_REQUESTS[index % len(CONGESTION_REQUESTS)]
            )
            streams.append(stream)
    except OSError:
        release_congestion(streams)
        raise
    return streams


def release_congestion(streams):
    """Close the staged connections — the submission lane's workers free
    with their pins."""
    for stream in streams:
        try:
            stream.close()
        except OSError:
            pass


def isolation_verdict(attributed, bodiless):
    """The leg's classification of its two control-lane samples under the
    staged congestion.

    `attributed` says the actor-attributed demote and promote each
    answered inside the declared bound with the switch's own verdict;
    `bodiless` says the bodiless control requests on the same congested
    monitors did.

    `held` is the contract: the attributed switch answers while the
    submission lane is pinned, because its body arrived already
    buffered and rode the control lane. `queued-behind` — the
    attributed switch never answered while a bodiless control request
    on the same monitor did — is the pre-contract routing, where every
    request carrying a body was quarantined on the submission lane, so
    the pinned release predates the contract. `control-lane-silent` —
    neither shape answered — is not the submission lane's isolation at
    all: the control lane itself never served, the leg's own staging
    broke the precondition, and the lateness says nothing about where
    the attributed body was routed."""
    if attributed:
        return "held"
    return "control-lane-silent" if not bodiless else "queued-behind"


def bounded_get(url, bound, attempts=1):
    """One bounded GET — `(body, error)`, `body` None where the read
    never answered inside `bound` on any of its `attempts`."""
    error = None
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=bound) as response:
                return json.load(response), None
        except Exception as exc:
            error = exc
    return None, error


def bounded_post(url, body, bound):
    """One bounded POST — `(status, decoded, error)`. `body` None
    submits the *bare* request, declaring no body at all; a body rides
    the declared `{"actor": …}` envelope. A refused request's named
    answer decodes rather than raising, so a `409` asserts its own
    verdict."""
    if body is None:
        request = urllib.request.Request(url, method="POST")
    else:
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
    try:
        with urllib.request.urlopen(request, timeout=bound) as response:
            return response.status, json.load(response), None
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            decoded = json.loads(raw)
        except ValueError:
            decoded = raw.decode(errors="replace")
        return error.code, decoded, None
    except Exception as exc:
        return None, None, exc


def answered_verdict(status, decoded):
    """The verdict name an answered request carries — a switch's served
    `RoleReport` role on an accepted `200`, the named refusal a `409`
    answers with (the `SwitchError` wire shape is a bare variant name,
    or a single-key object for a refusal carrying detail), or a
    truncated rendering of anything else with its status."""
    if status == 200 and isinstance(decoded, dict) and "role" in decoded:
        return decoded["role"]
    if isinstance(decoded, str) and decoded.strip():
        return decoded
    if status == 409 and isinstance(decoded, dict) and len(decoded) == 1:
        return next(iter(decoded))
    return f"{status} {json.dumps(decoded)[:200]}"


def switch_sample(url, body, bound):
    """One bounded switch request — the sample the isolation is read
    from. `body` None submits the bare request. Returns
    `(verdict, detail)` — `verdict` the answered verdict name, or None
    where the request never answered inside `bound`; `detail` the
    rendered request's own outcome, the failure's error where it never
    answered."""
    status, decoded, error = bounded_post(url, body, bound)
    if status is None:
        return None, (
            f"never answered inside the declared {bound}s bound: {error}"
        )
    return answered_verdict(status, decoded), json.dumps(decoded)[:300]


def rendered(value, error):
    """A served body's own rendering, or the error that answered none."""
    if value is None:
        return str(error)
    return json.dumps(value)[:200]


def liveness_sample(url, name, bound):
    """One peer's bounded `GET /health` and `GET /role` — the heartbeat
    lane's marks under the congestion: the served liveness role, the
    served role, and whether the tracking peer's convergence still
    stands. Returns `(marks, detail)`; a mark is None where its read
    never answered inside the declared bound."""
    health, health_error = bounded_get(
        f"{url}/health", bound, LIVENESS_ATTEMPTS
    )
    report, role_error = bounded_get(
        f"{url}/role", bound, LIVENESS_ATTEMPTS
    )
    marks = {"health": None, "role": None, "tracking": None}
    if isinstance(health, dict) and health.get("live") is True:
        marks["health"] = health.get("role")
    if isinstance(report, dict):
        marks["role"] = report.get("role")
        sync = report.get("sync")
        if isinstance(sync, dict) and "tracking" in sync:
            marks["tracking"] = True
    return marks, {
        "health": f"GET /health answered {rendered(health, health_error)}",
        "role": f"GET /role answered {rendered(report, role_error)}",
    }


def role_stream(path):
    """The durable journal file's `role_changed` entries as
    `(from, to, origin, actor)` tuples in `seq` order. A file still
    mid-write reads as the stream read so far: the drain is
    asynchronous and the caller polls."""
    stream = []
    try:
        records = pair.journal_records(path)
    except Abort:
        return stream
    for kind, record in records:
        if kind != "entry":
            continue
        change = record.get("event", {}).get("role_changed")
        if change is None:
            continue
        stream.append(
            (
                change.get("from"),
                change.get("to"),
                change.get("origin"),
                change.get("actor"),
            )
        )
    return stream


def attributed_records(path, floor, name, failures, deadline_s=15.0):
    """The peer's gained `role_changed` entries, polled until the
    asynchronous journal drain carries the switch's own — or the
    deadline passes. Asserts every gained entry names `origin: request`
    beside the leg's declared actor: an attributed switch answers as
    the attributed form and says who asked, and a record carrying no
    actor is the bare request silently dropping its body."""
    began = time.monotonic()
    while True:
        gained = role_stream(path)[floor:]
        if len(gained) >= 2 or time.monotonic() - began > deadline_s:
            break
        time.sleep(0.2)
    if len(gained) < 2:
        failures.append(
            f"{name}'s durable journal gained {len(gained)} role_changed "
            "entries across the attributed switch, expected the "
            "switch's two transitions"
        )
        return gained
    for frm, to, origin, actor in gained:
        if origin != "request" or actor != ACTOR:
            failures.append(
                f"{name}'s durable journal carries {frm}->{to} with "
                f"origin={origin!r} actor={actor!r}, expected the "
                f"declared actor {ACTOR!r} under origin 'request' — "
                "the attributed switch must journal who asked"
            )
    return gained


def attributed_switch_isolation_pass(args, tamper):
    """The attributed-switch-isolation run: converge the declared pair,
    record the settled control-plane baseline, stage the submission
    lane's congestion on both monitors, issue the attributed demote
    and promote under it, then release the pins and restore the launch
    roles. Returns `(digest_entries, evidence, failures)`; raises
    `Inconclusive` where the pinned release predates the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "attributed-switch-isolation leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    # The declared incident-time bound — the answer the control lane
    # owes an operator's attributed switch while the submission lane
    # stands pinned. The `starved-switch` tamper doctors it to zero,
    # so every control-lane sample under the congestion observes the
    # starvation the isolation exists to prevent.
    bound = 0.0 if tamper == "starved-switch" else SWITCH_BOUND_S
    digest_entries, evidence, failures = [], {}, []
    rig = None
    pins = []
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        duty_name, standby_name = (
            duty_decl["name"],
            standby_decl["name"],
        )
        journals = {
            duty_name: rig.duty_files["journal_file"],
            standby_name: rig.standby_files["journal_file"],
        }
        peers = (
            (duty_name, duty_url, "active"),
            (standby_name, standby_url, "standby"),
        )

        # Phase 1 — convergence on the declared pair: the field owner
        # `active`, the declared standby settled `tracking` it.
        converged = rig.converge(failures)
        evidence["converged"] = converged["ticks"][-1]
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty": "active",
                "standby": "standby/tracking",
            }
        )

        # Phase 2 — the settled baseline: each peer's liveness reads and
        # the *bodiless* control requests' own verdicts. The bare shape
        # is the control lane's pre-attribution form and routes
        # identically on every release, so it is the leg's control
        # witness through the congestion below.
        baseline = {}
        for name, url, want in peers:
            marks, detail = liveness_sample(url, name, LIVENESS_BOUND_S)
            if marks["health"] != want or marks["role"] != want:
                failures.append(
                    f"{name} reports {detail['health']} and "
                    f"{detail['role']} on the settled baseline, "
                    f"expected {want} on both"
                )
            if name == standby_name and marks["tracking"] is not True:
                failures.append(
                    "the tracking peer's served role report carries no "
                    f"tracking convergence on the settled baseline: "
                    f"{detail['role']}"
                )
            baseline[name] = marks
        if failures:
            raise Abort
        control = {}
        for name, url, endpoint, want in (
            (duty_name, duty_url, "promote", "already_active"),
            (standby_name, standby_url, "demote", "not_active"),
        ):
            verdict, detail = switch_sample(
                f"{url}/{endpoint}", None, LIVENESS_BOUND_S
            )
            control[name] = verdict
            if verdict != want:
                failures.append(
                    f"the bare POST /{endpoint} on {name} answered "
                    f"{verdict if verdict is not None else detail} on "
                    f"the settled baseline, expected the named {want} "
                    "refusal — the control-lane witness the leg reads "
                    "through the congestion is not serving"
                )
        if failures:
            raise Abort
        digest_entries.append(
            {"phase": "baseline", "peers": baseline, "control": control}
        )

        # Phase 3 — the congestion: the saturating set of severed
        # bodies against *both* peers' monitors — the field owner's the
        # demote targets, the tracking standby's the promote does —
        # each pinning a submission worker while its connection stays
        # open.
        for url in (duty_url, standby_url):
            pins.extend(stage_congestion(url))
        digest_entries.append(
            {
                "phase": "congest",
                "connections": len(pins),
                "shapes": list(CONGESTION_SHAPES),
            }
        )

        # Phase 4 — the pin is real: a driven scan on the congested
        # standby must not answer while the pinning set stands. The
        # zero-scan batch is the witness's own shape — a submission
        # request, so it queues like any other — whose late landing
        # runs no scan and so cannot disturb the run's own accounting.
        status, decoded, _error = bounded_post(
            f"{standby_url}/scan", {"scans": 0}, PIN_BOUND_S
        )
        if status is not None:
            raise Inconclusive(
                "the pinned release carries no pinnable submission lane",
                "a driven POST /scan answered "
                f"{answered_verdict(status, decoded)} inside "
                f"{PIN_BOUND_S}s while {len(pins)} severed bodies stood "
                "open against both monitors — the submission lane was "
                "never pinned, so the leg has no isolation to assert",
            )

        # Phase 5 — the incident-time actuation under the congestion.
        # The liveness reads and the bodiless control requests first,
        # still at the settled baseline nothing has moved: the
        # heartbeat lane keeps answering and the control lane keeps
        # serving its own pre-attribution form beside the pins.
        congestion = {}
        for name, url, want in peers:
            marks, detail = liveness_sample(url, name, LIVENESS_BOUND_S)
            congestion[name] = marks
            if marks["health"] != want or marks["role"] != want:
                failures.append(
                    f"{name} reports {detail['health']} and "
                    f"{detail['role']} under the staged congestion, "
                    f"expected {want} on both — the pair's liveness "
                    "did not stay at its baseline while the submission "
                    "lane stood pinned"
                )
            if name == standby_name and marks["tracking"] is not True:
                failures.append(
                    "the tracking peer lost its convergence under the "
                    f"staged congestion: {detail['role']}"
                )
        witness = {}
        for name, url, endpoint in (
            (duty_name, duty_url, "promote"),
            (standby_name, standby_url, "demote"),
        ):
            verdict, detail = switch_sample(
                f"{url}/{endpoint}", None, bound
            )
            witness[name] = (verdict, detail)
        floors = {
            name: len(role_stream(path)) for name, path in journals.items()
        }
        switch = {}
        details = {}
        attributed_ok = True
        for name, url, endpoint, want in (
            (duty_name, duty_url, "demote", "demoting"),
            (standby_name, standby_url, "promote", "promoting"),
        ):
            verdict, detail = switch_sample(
                f"{url}/{endpoint}", {"actor": ACTOR}, bound
            )
            switch[endpoint] = verdict
            details[endpoint] = detail
            if verdict is None:
                attributed_ok = False
            elif verdict != want:
                attributed_ok = False
                failures.append(
                    f"the attributed POST /{endpoint} on {name} "
                    f"answered {verdict} inside the declared {bound}s "
                    f"bound, expected the switch's own {want} report: "
                    f"{detail}"
                )
        if failures:
            raise Abort
        bodiless_ok = all(
            verdict is not None for verdict, _detail in witness.values()
        )
        verdict = isolation_verdict(attributed_ok, bodiless_ok)
        if verdict == "queued-behind":
            raise Inconclusive(
                "the pinned release predates the attributed-switch "
                "isolation",
                "the attributed demote/promote never answered inside "
                f"the declared {bound}s bound while {len(pins)} severed "
                "bodies stood open against both monitors "
                f"({details['demote']}; {details['promote']}) — while "
                "the bodiless control requests on the same monitors "
                "answered ("
                + f"{witness[duty_name][0]}; {witness[standby_name][0]}"
                + "): the release still quarantines every "
                "body-carrying request on the submission lane, so the "
                "attributed switch queued behind the pins",
            )
        if verdict == "control-lane-silent":
            raise Abort(
                "the attributed demote/promote never answered inside the "
                f"declared {bound}s bound behind the staged congestion "
                f"({details['demote']}; {details['promote']}) — and the "
                "bodiless control requests on the same monitors never "
                "answered either "
                f"({witness[duty_name][1]}; {witness[standby_name][1]}): "
                "neither form was served in that window, so the run has "
                "no control-lane answer to read the attributed switch's "
                "lane against — this lateness is not the submission "
                "lane's isolation"
            )

        # Phase 6 — the liveness reads keep answering through the
        # switch the congestion stood beside: the pair's own liveness
        # surface is not what the pinned submission lane can starve.
        served = {}
        for name, url, _want in peers:
            marks, detail = liveness_sample(url, name, LIVENESS_BOUND_S)
            served[name] = marks
            if marks["health"] is None or marks["role"] is None:
                failures.append(
                    f"{name}'s liveness reads stopped answering under "
                    f"the staged congestion: {detail['health']}; "
                    f"{detail['role']}"
                )
        digest_entries.append(
            {
                "phase": "isolation",
                "control": {
                    name: mark for name, (mark, _d) in witness.items()
                },
                "attributed": switch,
                "peers": congestion,
                "served": served,
            }
        )

        # Phase 7 — the release: closing the pins frees the submission
        # lane, tracking-first driven ticks settle the switch under it,
        # and the attributed demote/promote pair runs back to the launch
        # roles for the legs behind this one.
        release_congestion(pins)
        pins = []
        settle = []
        for _ in range(HANDOVER_TICKS):
            _tracked, owner = rig.tick(duty_url, standby_url, failures)
            settle.append(owner["tick"])
        promoted_role = pair.get(f"{standby_url}/role", "GET /role", failures)
        demoted_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        sync = demoted_role.get("sync")
        if promoted_role.get("role") != "active":
            failures.append(
                "the promoted peer reports "
                f"{promoted_role.get('role')!r} after the congestion, "
                "expected active — the switch the pinned submission "
                "lane stood beside did not settle"
            )
        if demoted_role.get("role") != "standby" or not (
            isinstance(sync, dict) and "tracking" in sync
        ):
            failures.append(
                "the demoted peer never reconverged after the "
                f"congestion — GET /role answers {demoted_role}"
            )
        # The durable record: each peer's role_changed entries gained
        # across the switch land on their own next scan boundary, so the
        # audit reads the journal after the settle — the request
        # transition and the settle transition, each naming the declared
        # actor under the requested origin.
        records = {
            name: attributed_records(
                journals[name], floors[name], name, failures
            )
            for name in (duty_name, standby_name)
        }
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "settle",
                "ticks": settle,
                "promoted": "active",
                "demoted": "standby/tracking",
                "journal": {
                    name: len(gained) for name, gained in records.items()
                },
            }
        )

        # The restore: the documented switch back, attributed like the
        # one it seats — re-arming the launch roles and the launch
        # owner's claim for the legs behind this one.
        for name, url, endpoint, want in (
            (standby_name, standby_url, "demote", "demoting"),
            (duty_name, duty_url, "promote", "promoting"),
        ):
            verdict, detail = switch_sample(
                f"{url}/{endpoint}", {"actor": ACTOR}, bound
            )
            if verdict != want:
                failures.append(
                    f"the restore's attributed POST /{endpoint} on "
                    f"{name} answered {detail}, expected the switch's "
                    f"own {want} report"
                )
        restore = []
        for _ in range(RESTORE_TICKS):
            _tracked, owner = rig.tick(standby_url, duty_url, failures)
            restore.append(owner["tick"])
        if not driver_recovery.roles_hold(
            rig, failures, "after releasing the congestion"
        ):
            raise Abort
        evidence["restored"] = restore[-1]
        digest_entries.append(
            {
                "phase": "restore",
                "ticks": restore,
                "duty": "active",
                "standby": "standby/tracking",
            }
        )
    except Inconclusive:
        raise
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        release_congestion(pins)
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
        choices=["starved-switch"],
        help="doctor the declared answer bound to zero so every "
        "control-lane sample under the staged congestion starves — "
        "the pass must fail naming the attributed switch that queued "
        "behind it",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = (
            attributed_switch_isolation_pass(args, args.tamper)
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                f"attributed-switch-isolation: {TAMPER_EVIDENCE} — the "
                "doctored zero bound; an inconclusive run offers the "
                "doctored case no evidence"
            )
            return 1
        # The digest line carries only the stable reason — the run's own
        # verdicts report on stderr, where two identical passes need not
        # share them.
        reason = inconclusive.args[0]
        detail = inconclusive.args[-1]
        eprint(f"attributed-switch-isolation: inconclusive — {detail}")
        print(
            f"attributed-switch-isolation-digest inconclusive — {reason}"
        )
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"attributed-switch-isolation: {line}")
        return 1
    for failure in failures:
        eprint(f"attributed-switch-isolation: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"attributed-switch-isolation: the {args.tamper} case "
                "passed silently — the leg never noticed the doctored "
                "bound"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    phases = {entry["phase"]: entry for entry in digest_entries}
    attributed = phases["isolation"]["attributed"]
    print(
        f"attributed-switch-isolation-digest {digest} — tracking by "
        f"tick {evidence['converged']}, the submission lane pinned by "
        f"{phases['congest']['connections']} severed bodies while the "
        f"attributed demote/promote answered {attributed['demote']}/"
        f"{attributed['promote']} inside the declared {SWITCH_BOUND_S}s "
        "bound beside the bodiless control requests' baseline verdicts "
        "and both peers' liveness reads, the durable journals naming "
        f"the declared actor on both peers, and the pair settling and "
        f"restoring its launch roles at tick {evidence['restored']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())