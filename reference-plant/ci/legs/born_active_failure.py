#!/usr/bin/env python3
"""The born-active startup-failure leg for the reference plant — the
consumer-side proof that #985's recorded startup-failure contract
(architecture decision 103, implemented under #1017) holds on the
manifest-declared redundant pair's launch path (WW-ENG-003,
WW-LCM-001): a controller launched *active* — the restart-as-active
shape the deployed pair's duty member runs — never exits for a
field-side startup condition, and each recorded class settles to its
recorded disposition on the served surface.

The pair leg (`ci/legs/pair.py`) proves the declared pair converges
and switches, and the startup-claim leg (`ci/legs/startup_claim.py`)
proves a doomed launch can never strand a claim over the incumbent.
This leg exercises the launch path itself: a born-active meeting
each of the contract's three recorded startup-failure classes while
the deployed pair keeps serving beside it. The classes, replayed
against a fresh launched active per class on the same
`dcs-controller --driven --remote` shape the pair runs:

- *field transport unreachable* — a born-active whose `--remote`
  plant answers nothing at boot stands pending behind the honest
  standby surface instead of dying in driver assembly: role
  `standby`, sync `unsynchronized`, `field_claim` absent (an
  unreachable field is unobserved, never `unclaimed`), commands
  refused `not_active`, the stand-down journaled under the
  field-arbitration origin, the backend diagnostics reporting
  `disconnected` with `last_error` — and the first answered field
  contact completes the deferred startup grant: `promoting` then
  `active`, the claim `held`, the landing journaled under the
  reclaim origin;
- *claim refused by a live incumbent* — a born-active launched
  against the settled pair's own plant with `--peer` naming the
  field owner is refused by the standing claim and rejoins the
  declared pair as its tracking standby: the refusal's observed
  claimant journaled, `field_claim` `held`, the run converging
  `tracking` on the incumbent's stream while the incumbent's role,
  writes, and claim stay undisturbed;
- *the degraded alternative — claim verdict inconclusive* — a
  born-active whose transport attaches but whose claim ask produces
  no verdict (an endpoint that accepts and resets unanswered) holds
  the same pending surface, never exiting and never rejoining,
  re-issuing the conditional grant on each answered contact until
  the field's return lands it `active`.

Each class runs while the deployed pair's members keep their launch
roles — the field owner `active`, the declared standby `tracking`,
the peers' images identical — and the leg leaves the pair exactly as
it found it for the legs behind this one.

The contract postdates releases the manifest may pin: a pinned
release predating #1017's implementation exits the launched active
where the contract keeps it pending or rejoined — a startup exit
naming the field-side refusal is reported `inconclusive`, never a
product failure, as is a served surface missing the contract's
substrate.

Usage:

    born_active_failure.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `born-active-failure-digest <sha256>` line prints —
the check runs two passes and compares them
(`born-active-failure-nondeterministic`). A contract violation
reports `born-active-failure: …` lines on stderr and exits 1 — the
check's `born-active-failure-failed`. `--tamper
expect-refusal-exit` doctors the refused class's assertion to the
wrong recorded response — the undeclared-pair exit where the
declared pair's rejoin is recorded — so the leg proves its
recorded-response assertion fires on the honest run.
"""

import argparse
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import driver_recovery
import failover
import pair
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored case.
# The doctored case: a leg asserting the refused claim's undeclared
# disposition — the exit — where the declared pair's rejoin is the
# recorded response must surface the named diagnostic on the honest
# rejoin rather than passing an unexercised contract.
LEG = {
    "order": 640,
    "title": "the born-active startup-failure leg",
    "passes": "born-active-failure-leg",
    "tampers": [
        {
            "name": "expect-refusal-exit",
            "passed": "an expect-refusal-exit case passed the born-active startup-failure leg",
            "missed": "the expect-refusal-exit case did not report its named diagnostic",
            "evidence": [
                "the doctored expectation wanted the refused launch to exit"
            ],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The pinned release predates the born-active startup-failure
    contract — a launch exiting on a field-side startup condition is
    the pre-#1017 behavior — or the served substrate the leg's
    evidence reads from is absent: the run classifies inconclusive,
    never a product failure."""


# The driven-scan bounds: the pending window a deferred grant waits
# through unanswered contacts, the rejoin window the refused
# rejoin converges `tracking` inside, and the settle ticks the
# restore proves launch roles on. The wall-clock wait each field
# arrival gives the remote driver's bounded re-attach — the
# connection's one-second re-attach spacing plus margin.
PENDING_SCANS = 3
REJOIN_SCANS = 6
SETTLE_TICKS = 3
REATTACH_WAIT = 1.4
ACTOR = "ci-born-active-failure"

# The stderr vocabulary a launch's field-side exit names — the
# pre-contract refusal surface (`cannot connect to plant`,
# `FieldClaimFailed`, the `--standby` remedy, an unrecognized
# contract flag). An exit naming one of these is the pinned release
# predating the contract; an exit naming nothing is a defect.
PREDATING_WORDS = (
    "cannot connect",
    "claim",
    "rejoin",
    "write-ownership",
    "unknown option",
    "unrecognized",
)


class Blackhole:
    """The unanswered-claim endpoint for the inconclusive class: a
    listener that accepts every connection and resets it
    unanswered — the transport attach succeeding while the claim
    ask never produces a verdict, the `Err` leg the pending state
    re-issues the conditional grant against. Resetting rather than
    closing leaves no server-side TIME_WAIT on the port, so the
    returning field rebinds the same address cleanly."""

    def __init__(self, address):
        host, _, port = address.rpartition(":")
        self.address = (host, int(port))
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(self.address)
        listener.listen()
        self._listener = listener
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._reset, daemon=True)
        self._thread.start()

    def _reset(self):
        self._listener.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except OSError:
                continue
            try:
                conn.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_LINGER,
                    struct.pack("ii", 1, 0),
                )
            except OSError:
                pass
            conn.close()

    def close(self):
        """Release the endpoint — the field's return rebinds the
        address."""
        self._stop.set()
        try:
            socket.create_connection(self.address, timeout=0.2).close()
        except OSError:
            pass
        self._thread.join(timeout=2)
        self._listener.close()


def spawn_born_active(args, plant_addr, files, peer=None):
    """Spawn a launched active on the deployed pair's launch shape —
    `dcs-controller <model> --remote <plant> --driven --pair-token`,
    `peer` the declared pair's `--peer` argument a restart-as-active
    names for its refusal rejoin, `files` the launched run's
    `--journal-file`/`--state-file` under the leg's runner-owned
    scratch. Returns `(process, monitor_url, preamble)` like
    `pair.spawn_peer` — `monitor_url` None when the process exits
    before reporting a listener, the preamble then carrying the
    startup refusal's stderr lines."""
    argv = [
        args.controller,
        args.model,
        "--remote",
        plant_addr,
        "--driven",
        "--listen",
        "127.0.0.1:0",
        "--dt",
        str(args.dt),
        "--pair-token",
        pair.PAIR_TOKEN,
    ]
    if peer is not None:
        argv += ["--peer", peer]
    for field, flag in (
        ("journal_file", "--journal-file"),
        ("state_file", "--state-file"),
    ):
        if files.get(field) is not None:
            argv += [flag, files[field]]
    process = subprocess.Popen(argv, stderr=subprocess.PIPE, text=True)
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


def spawn_or_classify(label, args, plant_addr, files, peer=None):
    """`spawn_born_active` plus the release gate: under the contract
    a launched active never exits for a field-side startup
    condition, so an exit naming the field-side refusal vocabulary
    is the pinned release predating #1017 — inconclusive — while an
    exit naming nothing is a defect."""
    process, url, preamble = spawn_born_active(
        args, plant_addr, files, peer=peer
    )
    if url is not None:
        return process, url, preamble
    joined = " ".join(preamble)
    if any(word in joined for word in PREDATING_WORDS):
        raise Inconclusive(
            "the pinned release predates the born-active "
            f"startup-failure contract — {label} exited at startup "
            f"naming a field-side refusal: "
            f"{'; '.join(preamble[-2:]) or 'no diagnostic'}"
        )
    raise Abort(
        f"{label} exited at startup without a field-side refusal: "
        f"{'; '.join(preamble) or 'no diagnostic'}"
    )


def spawn_plant_at(args, address):
    """A `dcs-plant-server` bound at `address` — the late-arriving
    field a pending launch's deferred grant retries toward."""
    process = subprocess.Popen(
        [
            args.plant_server,
            args.model,
            "--dynamics",
            args.dynamics,
            "--listen",
            address,
        ],
        stderr=subprocess.PIPE,
        text=True,
    )
    bound = simulate.listen_address(process, "dcs-plant-server")
    if bound != address:
        pair.stop(process)
        raise Abort(
            f"the late-arriving field bound {bound}, not the pending "
            f"launch's {address} — the completion half cannot land"
        )
    return process


def role(url, failures):
    """The peer's served RoleReport."""
    return pair.get(f"{url}/role", "GET /role", failures)


def journal(url, failures):
    """The peer's served journal entries."""
    return pair.get(f"{url}/journal", "GET /journal", failures)


def transitions(entries):
    """The `(from, to, origin)` role transitions a served journal
    carries, in `seq` order."""
    return [
        (
            change["from"],
            change["to"],
            change.get("origin"),
        )
        for entry in entries
        for change in [entry.get("event", {}).get("role_changed")]
        if change is not None
    ]


def refuse_check(url, command, failures):
    """The pending/standby command boundary: `POST /command` must
    answer a rejected `not_active` receipt — a run that has never
    owned the field admits no receipts for it."""
    status, refusal = pair.request(
        f"{url}/command", {"command": command, "actor": ACTOR}
    )
    reason = (
        refusal.get("outcome", {}).get("rejected", {}).get("reason", {})
        if isinstance(refusal, dict)
        else {}
    )
    if status != 200 or "not_active" not in reason:
        failures.append(
            f"the pending run's command boundary answered {status} "
            f"{refusal}, expected a rejected not_active receipt"
        )
        return None
    return "refused-not-active"


def assert_pending(label, url, preamble, command, snapshot, failures):
    """The recorded pending-claim surface — the same honest standby
    report the unreachable-field and inconclusive-claim classes
    serve: `standby`/`unsynchronized`, `field_claim` absent (an
    unreachable field is unobserved, never `unclaimed`), commands
    refused `not_active`, the stand-down journaled under the
    field-arbitration origin, and the backend diagnostics reporting
    `disconnected` while io_health counts the per-point faults the
    silent field's accesses produce. Returns the class's normalized
    marks."""
    report = role(url, failures)
    if (
        report.get("role") != "standby"
        or report.get("sync") != "unsynchronized"
        or report.get("field_claim") is not None
    ):
        failures.append(
            f"{label}: the pending surface reads {report} — expected "
            "role standby, sync unsynchronized, no observed claim"
        )
    if not any("stands pending" in line for line in preamble):
        failures.append(
            f"{label}: the startup log carries no pending report: "
            f"{preamble}"
        )
    marks = {
        "surface": "standby/unsynchronized/no-claim",
        "command": refuse_check(url, command, failures),
        "link": None,
        "stand_down": None,
    }
    health = (snapshot or {}).get("io_health")
    driver = driver_recovery.driver_health(snapshot)
    if health is None or driver is None:
        raise Inconclusive(
            f"{label}: the pending run serves no backend driver "
            "diagnostics — the pinned release predates the "
            "remote-driver contract the pending surface reports on"
        )
    # The pending run's disconnected-field report: the backend
    # volunteers `disconnected`, the per-point reads its quiesced
    # scans attempt answer Disconnected — `failed_reads` counting
    # and `last_error` recording the fault — while `driver.last_error`
    # stays empty, a write-quiesced run never having put a write on
    # the dead transport. The standing-outage degraded serve — a
    # write through the dead link — is driver_recovery's stronger
    # shape; the startup class's is this one.
    error = (health.get("last_error") or {}).get("error") or {}
    if (
        driver.get("link") != "disconnected"
        or "disconnected" not in error
        or (health.get("failed_reads") or 0)
        + (health.get("failed_writes") or 0)
        == 0
    ):
        failures.append(
            f"{label}: the pending run's backend diagnostics are not "
            "the disconnected-named report the contract owes: "
            f"{json.dumps(health)[:300]}"
        )
    else:
        marks["link"] = "disconnected-named"
    stood_down = any(
        frm == "active" and to == "standby" and origin == "fenced"
        for frm, to, origin in transitions(journal(url, failures))
    )
    if not stood_down:
        failures.append(
            f"{label}: the stand-down transition active->standby "
            "under the field-arbitration origin never journaled"
        )
    else:
        marks["stand_down"] = "journaled"
    return marks


def assert_grant_landed(label, url, failures):
    """The deferred startup grant's completion once the field
    answers: driven scans — each re-attach bounded by the remote
    driver's spacing — until the pending run reports `promoting` or
    `active`, then `active` with the claim `held` and the
    standby->promoting->active walk journaled under the reclaim
    origin. Returns the normalized marks."""
    report = None
    for _ in range(REJOIN_SCANS):
        pair.scan(url, failures)
        report = role(url, failures)
        if report.get("role") in ("promoting", "active"):
            break
        # The re-attach spacing is wall-clock while the scans are
        # driven — let the next scan's contact actually retry.
        time.sleep(REATTACH_WAIT)
    if report is not None and report.get("role") == "promoting":
        pair.scan(url, failures)
        report = role(url, failures)
    if report is None or report.get("role") != "active":
        failures.append(
            f"{label}: the first answered contact did not complete "
            f"the deferred startup grant — GET /role answers {report}"
        )
        return {"settled": None, "claim": None, "landing": None}
    if report.get("field_claim") != "held":
        failures.append(
            f"{label}: the landed grant reports field_claim "
            f"{report.get('field_claim')!r}, expected held"
        )
    walked = [(frm, to) for frm, to, _origin in transitions(journal(url, failures))]
    origins = {
        (frm, to): origin
        for frm, to, origin in transitions(journal(url, failures))
    }
    if ("standby", "promoting") not in walked or (
        "promoting", "active"
    ) not in walked:
        failures.append(
            f"{label}: the grant's standby->promoting->active walk "
            f"never journaled: {walked}"
        )
    elif origins.get(("standby", "promoting")) != "reclaim":
        failures.append(
            f"{label}: the deferred grant's landing journaled under "
            f"{origins.get(('standby', 'promoting'))!r}, expected "
            "the reclaim origin — the field's conditional ask "
            "finally answered"
        )
    return {
        "settled": "active",
        "claim": "held" if report.get("field_claim") == "held" else None,
        "landing": "journaled-reclaim",
    }


def pair_health(rig, failures, when):
    """The deployed pair's launch roles and identical images mid-leg
    — one tracking-first pair tick plus the role poll: the field
    owner `active`, the declared standby `tracking`. A moved role or
    a diverged image is a peer the class replay wedged."""
    held = driver_recovery.roles_hold(rig, failures, when)
    rig.tick(rig.standby_url, rig.duty_url, failures)
    return "active+tracking" if held else "moved"


def scratch_files(rig, name):
    """The launched member's persistence under the rig's
    runner-owned scratch — the durable journal the stand-down and
    verdict records append to."""
    root = os.path.join(rig.scratch, name)
    os.makedirs(root, exist_ok=True)
    return {
        "journal_file": os.path.join(root, "journal.jsonl"),
        "state_file": None,
    }


def born_active_failure_pass(args, tamper):
    """The born-active startup-failure run: converge the deployed
    pair, then replay each recorded class on a fresh launched active
    — unreachable field, refused claim on the declared pair, the
    inconclusive verdict — asserting the recorded response and named
    evidence per class while the pair's launch roles hold, then
    restore. Returns `(digest_entries, evidence, failures)`; raises
    `Inconclusive` where the pinned release predates the
    contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "born-active-failure leg has nothing to exercise"
        )
    _manifest, _duty_decl, standby_decl = declared
    budget = standby_decl.get("failover_budget")
    if not isinstance(budget, int) or isinstance(budget, bool) or budget < 1:
        raise Abort(
            "the manifest's standby declares no positive "
            "failover_budget — the deployed pair's armed heartbeat "
            "is absent"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = None
    members, fields = [], []
    blackhole = None
    probe_io = None
    try:
        rig = pair.launch_pair(args, declared, auto_promote=budget)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        probe_io = simulate.PlantClient(rig.plant_addr)

        # Phase 1 — convergence and the substrate gate: the settled
        # pair the classes replay beside — the incumbent's claim
        # line naming the owner token the refused class's
        # field_claim_observed attributes, the armed budget the
        # deployment carries. A duty that never reported its startup
        # claim is the release predating the contract's substrate.
        converged = rig.converge(failures)
        evidence["converged"] = converged["ticks"][-1]
        incumbent_token = failover.owner_token(rig.duty_preamble)
        if incumbent_token is None:
            raise Inconclusive(
                "the field owner's startup log carries no "
                "write-ownership claim line — the pinned release "
                "predates the startup-claim record the refused "
                "class's attribution reads"
            )
        with open(args.model) as handle:
            model = json.load(handle)
        points = failover.signal_points(model)
        if points is None:
            raise Abort(
                "the emitted model declares no p101-cmd/level-primary "
                "signal points — the leg has no field output to "
                "watch"
            )
        schema = pair.get(f"{duty_url}/schema", "GET /schema", failures)
        declared_cmd = pair.declared_command(schema)
        if declared_cmd is None:
            raise Abort(
                "the served registry declares no command — the "
                "pending surface's not_active refusal has nothing "
                "to refuse"
            )
        component, spec = declared_cmd
        command = {
            "invoke": {
                "component": component,
                "command": spec["name"],
                "arguments": simulate.command_arguments(spec),
            }
        }
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "incumbent": "active",
                "standby": "tracking",
                "armed": budget,
            }
        )

        # Phase 2 — the unreachable field: a born-active whose
        # --remote plant answers nothing at boot stands pending
        # instead of dying in driver assembly, holds the pending
        # surface through unanswered contacts, and the first
        # answered contact — the field's own arrival — completes the
        # deferred grant.
        dead_addr = pair.closed_port()
        unreachable, unreachable_url, unreachable_preamble = (
            spawn_or_classify(
                "the unreachable-field launch",
                args,
                dead_addr,
                scratch_files(rig, "unreachable"),
            )
        )
        members.append(unreachable)
        pending = assert_pending(
            "unreachable field",
            unreachable_url,
            unreachable_preamble,
            command,
            pair.scan(unreachable_url, failures),
            failures,
        )
        for _ in range(PENDING_SCANS):
            pair.scan(unreachable_url, failures)
            report = role(unreachable_url, failures)
            if (
                report.get("role") != "standby"
                or report.get("sync") != "unsynchronized"
                or report.get("field_claim") is not None
            ):
                failures.append(
                    "unreachable field: the pending run moved off "
                    f"the standby surface while the field stayed "
                    f"silent — GET /role answers {report}"
                )
        marks = dict(pending)
        marks["pair"] = pair_health(
            rig, failures, "through the unreachable-field class"
        )
        # The field's return: the re-attach spacing gates the retry,
        # then the deferred grant lands the pending run active.
        fields.append(spawn_plant_at(args, dead_addr))
        marks.update(
            assert_grant_landed(
                "unreachable field", unreachable_url, failures
            )
        )
        if failures:
            raise Abort
        digest_entries.append({"phase": "unreachable-field", **marks})
        pair.stop(members.pop())
        pair.stop(fields.pop())

        # Phase 3 — the refused claim: a born-active launched
        # against the settled pair's own plant — the field a live
        # incumbent already claims — declaring `--peer` so the
        # refusal's named remedy runs configured: it stands rejoined
        # as the pair's standby rather than exiting or standing
        # unpaired, tracking the incumbent it named while the
        # incumbent's role, writes, and claim stay undisturbed.
        refused, refused_url, refused_preamble = spawn_or_classify(
            "the refused-claim launch",
            args,
            rig.plant_addr,
            scratch_files(rig, "refused"),
            peer=duty_url.removeprefix("http://"),
        )
        members.append(refused)
        report = role(refused_url, failures)
        if (
            report.get("role") != "standby"
            or report.get("field_claim") != "held"
        ):
            failures.append(
                f"refused claim: the rejoined surface reads {report} "
                "— expected role standby behind the incumbent's "
                "observed held claim"
            )
        if not (
            any(
                "rejoined as standby" in line
                for line in refused_preamble
            )
            and any(
                "a live peer holds the field's write-ownership claim"
                in line
                for line in refused_preamble
            )
        ):
            failures.append(
                "refused claim: the startup log carries neither the "
                "incumbent's refusal nor the declared-pair rejoin: "
                f"{refused_preamble}"
            )
        marks = {
            "surface": "standby/held",
            "rejoined": "declared-pair",
            "command": refuse_check(refused_url, command, failures),
            "observed": None,
            "sync": None,
            "incumbent": None,
            "probe": None,
        }
        entries = journal(refused_url, failures)
        stood_down = any(
            frm == "active" and to == "standby" and origin == "fenced"
            for frm, to, origin in transitions(entries)
        )
        observed = [
            entry["event"]["field_claim_observed"]
            for entry in entries
            if "field_claim_observed" in entry.get("event", {})
        ]
        if not stood_down:
            failures.append(
                "refused claim: the stand-down transition "
                "active->standby under the field-arbitration origin "
                "never journaled"
            )
        if not any(
            record.get("claimant") == incumbent_token
            for record in observed
        ):
            failures.append(
                "refused claim: the incumbent's refusal never "
                "attributed — the journaled field_claim_observed "
                f"records are {observed}, expected claimant "
                f"{incumbent_token}"
            )
        else:
            marks["observed"] = "incumbent"
        if failures:
            raise Abort
        converged_sync = None
        for _ in range(REJOIN_SCANS):
            pair.scan(refused_url, failures)
            report = role(refused_url, failures)
            if report.get("role") != "standby":
                failures.append(
                    "refused claim: the rejoined run left standby — "
                    f"GET /role answers {report}"
                )
                raise Abort
            sync = report.get("sync")
            if isinstance(sync, dict) and (
                "tracking" in sync or "reinitialized" in sync
            ):
                converged_sync = sync
                break
        if converged_sync is None:
            failures.append(
                "refused claim: the declared pair never re-paired — "
                f"the rejoined run's sync stayed {report.get('sync')}"
            )
        else:
            marks["sync"] = "tracking"
        held = failover.field_read(probe_io, points["cmd"], failures)[
            "value"
        ]
        marks["probe"] = failover.probe_kind(
            failover.foreign_probe(probe_io, points["cmd"], held)
        )
        if marks["probe"] != "fenced":
            failures.append(
                "refused claim: the incumbent's claim no longer "
                f"fences the field — a foreign probe answered "
                f"{marks['probe']}"
            )
        incumbent = role(duty_url, failures)
        if incumbent.get("role") != "active":
            failures.append(
                "refused claim: the incumbent left role=active — "
                "the refused run preempted it"
            )
        else:
            marks["incumbent"] = "active"
        marks["pair"] = pair_health(
            rig, failures, "through the refused-claim class"
        )
        if failures:
            raise Abort
        if tamper == "expect-refusal-exit":
            # The doctored expectation — the wrong recorded response:
            # the undeclared-pair exit where the declared pair's
            # rejoin is the contract. The honest rejoined run must
            # fail it.
            failures.append(
                "the doctored expectation wanted the refused launch "
                "to exit — the recorded response rejoins the "
                "declared pair, and the honest run rejoined it"
            )
            raise Abort
        digest_entries.append({"phase": "refused-claim", **marks})
        pair.stop(members.pop())

        # Phase 4 — the inconclusive verdict: a born-active whose
        # transport attaches but whose claim ask produces no verdict
        # holds the same pending surface — never exiting, never
        # rejoining — re-issuing the conditional grant on each
        # answered contact until the field's return lands it.
        silent_addr = pair.closed_port()
        blackhole = Blackhole(silent_addr)
        silent, silent_url, silent_preamble = spawn_or_classify(
            "the inconclusive-claim launch",
            args,
            silent_addr,
            scratch_files(rig, "inconclusive"),
        )
        members.append(silent)
        pending = assert_pending(
            "inconclusive claim",
            silent_url,
            silent_preamble,
            command,
            pair.scan(silent_url, failures),
            failures,
        )
        for _ in range(PENDING_SCANS):
            pair.scan(silent_url, failures)
            report = role(silent_url, failures)
            if silent.poll() is not None:
                failures.append(
                    "inconclusive claim: the unanswered ask exited "
                    "the run — the contract holds it pending, "
                    "re-issuing per answered contact"
                )
                raise Abort
            if (
                report.get("role") != "standby"
                or report.get("sync") != "unsynchronized"
                or report.get("field_claim") is not None
            ):
                failures.append(
                    "inconclusive claim: the pending run moved off "
                    "the standby surface while no verdict stood — "
                    f"GET /role answers {report}"
                )
        marks = dict(pending)
        marks["held"] = "pending"
        marks["pair"] = pair_health(
            rig, failures, "through the inconclusive-claim class"
        )
        blackhole.close()
        blackhole = None
        fields.append(spawn_plant_at(args, silent_addr))
        marks.update(
            assert_grant_landed(
                "inconclusive claim", silent_url, failures
            )
        )
        if failures:
            raise Abort
        digest_entries.append({"phase": "inconclusive-claim", **marks})
        pair.stop(members.pop())
        pair.stop(fields.pop())

        # Phase 5 — the restore: every launched member is gone and
        # the deployed pair rests on its launch roles — the field
        # owner active, the declared standby tracking it — for the
        # legs behind this one.
        for _ in range(SETTLE_TICKS):
            rig.tick(standby_url, duty_url, failures)
        if not driver_recovery.roles_hold(
            rig, failures, "after the born-active classes"
        ):
            raise Abort
        digest_entries.append(
            {
                "phase": "restore",
                "duty": "active",
                "standby": "tracking",
            }
        )
        evidence["final_tick"] = role(duty_url, failures).get("tick")
    except Inconclusive:
        raise
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        if blackhole is not None:
            blackhole.close()
        for process in members:
            pair.stop(process)
        for process in fields:
            pair.stop(process)
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
        choices=["expect-refusal-exit"],
        help="doctor the refused class's assertion to the wrong "
        "recorded response — the undeclared-pair exit where the "
        "declared pair's rejoin is recorded — so the pass must "
        "fail naming it",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = born_active_failure_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "born-active-failure: the doctored expectation "
                "wanted the refused launch to exit — an inconclusive "
                "run offers the doctored case no evidence"
            )
            return 1
        eprint(f"born-active-failure: inconclusive — {inconclusive}")
        print(
            "born-active-failure-digest inconclusive — "
            f"{inconclusive}"
        )
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"born-active-failure: {line}")
        return 1
    for failure in failures:
        eprint(f"born-active-failure: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"born-active-failure: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored "
                "expectation"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"born-active-failure-digest {digest} — tracking by tick "
        f"{evidence['converged']}, all three recorded classes "
        "settled to their recorded responses on the launched pair "
        "path, launch roles held at tick "
        f"{evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
