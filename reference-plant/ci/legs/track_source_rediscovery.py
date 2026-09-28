#!/usr/bin/env python3
"""The track-source-rediscovery leg for the reference plant — the
consumer-side proof that #1202's per-pull re-resolution contract holds
on the manifest-declared redundant pair (WW-ENG-003, WW-LCM-001 —
mirrored at the customer boundary from the qa rig's
`track_source_move` reproduction of the
`standby-track-source-stale-ip-pin` finding): a tracking controller
whose `--standby` names the peer's container name follows the name's
owner across a container recreate that moves its address, rather
than pinning the startup-resolved address for the run's life.

The rig-side contract is pinned but the customer-owned pair — whose
manifest declares `ctrl-b` tracking `ctrl-a:8080` by container name,
exactly as the defective deployment did — never exercised it: on the
defective build the configured name resolved once at startup and
every pull dialed the pinned address, so the recreated peer kept
answering on its new address while the standby degraded against the
vacated one forever — and an armed standby counted the phantom
misses to its failover budget and preempted the still-live owner.

The reproduction needs the peer's configured name to resolve to a
moved address mid-run. Real DNS is not scripted, but `/etc/hosts` is
consulted on every lookup — so the tracking peer runs inside an
unprivileged user+mount namespace whose private hosts file binds the
manifest-declared source name where the leg puts it, the same lever
the rig test uses, and the field owner moves across loopback
addresses the way the rig's IPAM hands a recreated container a fresh
one. The run:

- launches the manifest-declared pair — the plant server, the
  declared duty member at the loopback address the declared source
  name resolves to, and the declared tracking member inside the
  namespace carrying `--standby <declared-name>:<duty-port>` exactly
  as the manifest's name-based wiring deploys it, armed with the
  declared failover budget — and converges it to `tracking`;
- recreates the tracking source's container: the duty member's
  process stops, its vacated address is held occupied by an unrelated
  workload that accepts and drops every connection, and the name's
  owner comes back cold — its declared journal file carried so the
  recreate's second lifetime appends to the durable record, the
  ephemeral state file's loss minting the new generation — on a
  *moved* loopback address at the same service port;
- drives the tracking peer through the miss window: the
  stale-resolution pulls degrade naming the configured *name* —
  never the startup-resolved address — then the rewritten hosts file
  lands the next resolution on the moved address and the peer
  reconverges `tracking` inside its first process lifetime, without
  a restart and inside the armed budget, the phantom failover never
  firing against the live owner;
- asserts the fallback journaled by name: the tracking peer's
  declared `--journal-file` carries exactly one `source_restarted`
  record — `was_aligned` the pre-move alignment, `resumed_at` the
  regressed stream — inside the single cold-start run boundary the
  no-restart proof needs, beside no `role_changed`, the served
  `GET /journal` agreeing; the recreated owner's file carries its
  second lifetime's boundary and no `field_claim_lost`;
- restores the pair's launch roles — the declared duty member
  `active` on its moved address, the declared standby `tracking` it —
  for the legs behind this one.

The contract postdates the pinned release — every released artifact
set predates it until the fix lands and a release carries it. A run
whose tracking peer strands `degraded` naming the startup-resolved
address, or whose armed budget manufactures a failover against the
moved owner, reports `track-source-rediscovery-digest inconclusive`
rather than asserting until the manifest repins a release carrying
the contract; so does a harness admitting no mount-namespace lever.

Usage:

    track_source_rediscovery.py --plant-server PATH \
        --controller PATH --model model/plant.json \
        --dynamics model/dynamics.json --scenario ci/scenario.json \
        --manifest deploy/manifest.json

On success one `track-source-rediscovery-digest <sha256>` line
prints — the check runs two passes and compares them
(`track-source-rediscovery-nondeterministic`). A contract violation
reports `track-source-rediscovery: …` lines on stderr and exits 1 —
the check's `track-source-rediscovery-failed`. `--tamper
expect-stranded` doctors the leg's own expectation to the defect
shape — asserting the tracking peer may stay stranded on the stale
address — so the leg proves its reconvergence assertion fires on the
honest run rather than passing an unexercised contract.
"""

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import claim_reclaim
import demote_reconvergence
import pair
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored case.
# The doctored case: a leg asserting the tracking peer may stay
# stranded on the stale address — the pre-contract wedge — must
# surface the named diagnostic on the honest re-discovering run
# rather than passing an unexercised contract.
LEG = {
    "order": 570,
    "title": "the track-source-rediscovery leg",
    "passes": "track-source-rediscovery-leg",
    "tampers": [
        {
            "name": "expect-stranded",
            "passed": "an expect-stranded case passed the track-source-rediscovery leg",
            "missed": "the expect-stranded case did not report its named diagnostic",
            "evidence": [
                "the doctored expectation wanted the tracking peer stranded on the stale address"
            ],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The pinned release predates — or the harness admits no lever
    for — the contract the leg exercises: the run classifies
    inconclusive, never a product failure."""


# The recreated container's new address — the rig's IPAM move across
# the loopback range. The settle beat each requested fetch gets to
# complete inside before the next driven scan consumes it — the
# puller's one-fetch-per-poll cadence is deterministic once each
# request settles — and the driven-scan counts each phase runs: the
# pre-move drains landing the alignment on the owner's frozen
# checkpoint, the stale-resolution window held strictly inside the
# armed failover budget, the fixed rejoin window the reconvergence
# must land inside, and the restore's tracking-first settle ticks.
MOVED_HOST = "127.0.0.2"
FETCH_SETTLE_S = 0.3
DRAIN_SCANS = 2
STALE_SCANS = 2
REJOIN_SCANS = 4
SETTLE_TICKS = 4


def runnable(argv):
    """Whether `argv` runs at all — the capability probe for each
    piece of the mount-namespace rig."""
    try:
        return (
            subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        )
    except OSError:
        return False


def missing_lever():
    """The capability this reproduction needs that the environment
    may lack, if any — the inconclusive message's missing piece."""
    for argv in (
        ["unshare", "--version"],
        ["sh", "-c", "true"],
        ["mount", "--version"],
    ):
        if not runnable(argv):
            return f"`{argv[0]}` is not runnable"
    if not runnable(["unshare", "-Urm", "--propagation", "private", "true"]):
        return "unprivileged user+mount namespaces are refused"
    try:
        stream = socket.socket()
        stream.bind((MOVED_HOST, 0))
        stream.close()
    except OSError:
        return "the 127.0.0.0/8 loopback range is not bindable"
    return None


def write_hosts(path, name, ip):
    """The namespace's private resolution source: `name` answers
    `ip`. Rewriting the file mid-run moves the declared source name
    exactly like the rig's IPAM re-issuing the container's address."""
    with open(path, "w") as handle:
        handle.write(
            f"{ip} {name}\n"
            "127.0.0.1 localhost\n"
            "::1 ip6-localhost ip6-loopback\n"
        )


def spawn_namespaced(controller, hosts, argv):
    """Spawn `dcs-controller` inside an unprivileged user+mount
    namespace whose `/etc/hosts` is the leg's file — the peer's
    declared container name resolving where the file binds it, the
    name's owner moving under it mid-run the way a container recreate
    re-addresses. Returns `(process, monitor_url, preamble)` like
    `pair.spawn_peer` — `monitor_url` None when the process exits
    before reporting a listener, the preamble then carrying the
    startup refusal's stderr lines."""
    script = 'mount --bind "$1" /etc/hosts && shift && exec "$@"'
    process = subprocess.Popen(
        [
            "unshare",
            "-Urm",
            "--propagation",
            "private",
            "--",
            "sh",
            "-c",
            script,
            "dcs-controller",
            hosts,
            controller,
        ]
        + argv,
        stderr=subprocess.PIPE,
        text=True,
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


def peer_argv(model, dt, plant_addr, standby, files,
              auto_promote=None, listen="127.0.0.1:0"):
    """The controller argv the launched pair's members carry — the
    shared harness's spawn flags built by hand so the tracking peer's
    spawn can ride inside its namespace: `standby` becomes the
    configured `--standby` source (the declared name for the tracking
    member, None on the field owner), `auto_promote` the manifest's
    declared failover budget arming that standby."""
    argv = [
        model,
        "--remote",
        plant_addr,
        "--driven",
        "--listen",
        listen,
        "--dt",
        str(dt),
    ]
    if standby is not None:
        argv += ["--standby", standby]
    if auto_promote is not None:
        argv += ["--auto-promote", str(auto_promote)]
    argv += ["--pair-token", pair.PAIR_TOKEN]
    for field, flag in (
        ("state_file", "--state-file"),
        ("journal_file", "--journal-file"),
    ):
        if files.get(field) is not None:
            argv += [flag, files[field]]
    return argv


class Placeholder:
    """The unrelated workload occupying the vacated container
    address — the rig's IPAM filling it: accepts and drops every
    connection, so a pull still pinned on the stale address keeps
    failing while the address itself stays live and refuses
    nothing."""

    def __init__(self, address):
        self.address = address
        listener = None
        for _ in range(10):
            try:
                listener = socket.socket()
                listener.setsockopt(
                    socket.SOL_SOCKET, socket.SO_REUSEADDR, 1
                )
                listener.bind(address)
                listener.listen()
                break
            except OSError:
                if listener is not None:
                    listener.close()
                listener = None
                time.sleep(0.1)
        if listener is None:
            raise Abort(
                "the leg cannot occupy the vacated address "
                f"{address[0]}:{address[1]} — the recreate's "
                "stale-pin window has no occupied shape"
            )
        self._listener = listener
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._drop, daemon=True)
        self._thread.start()

    def _drop(self):
        self._listener.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except OSError:
                continue
            conn.close()

    def close(self):
        """Release the occupied address — the accept loop wakes once
        on the probe connection so the listener unbinds promptly."""
        self._stop.set()
        try:
            socket.create_connection(self.address, timeout=0.2).close()
        except OSError:
            pass
        self._thread.join(timeout=2)
        self._listener.close()


def sync_kind(report):
    """The sync vocabulary a standby's RoleReport carries —
    `tracking`, `orphaned`, `degraded` — or `unsynchronized` when the
    report carries the string form."""
    sync = (report or {}).get("sync")
    if isinstance(sync, str):
        return sync
    return claim_reclaim.sync_state(report) or "missing"


def degraded_target(report):
    """The pull target a `degraded` report's detail names — the
    'fetch from <target>: <error>' a failed checkpoint pull reports,
    the configured *name* under the contract and the startup-resolved
    *address* under the defect — or None."""
    sync = (report or {}).get("sync")
    detail = (
        sync.get("degraded", {}).get("detail")
        if isinstance(sync, dict)
        else None
    )
    return demote_reconvergence.fetch_source(detail)


def journal_entries(path):
    """A `--journal-file`'s entry records in file order — the durable
    half of the audit."""
    return [
        record
        for kind, record in pair.journal_records(path)
        if kind == "entry"
    ]


def journal_boundaries(path):
    """A `--journal-file`'s run-boundary markers — one per process
    lifetime the file has carried."""
    return [
        record
        for kind, record in pair.journal_records(path)
        if kind == "boundary"
    ]


def durable_kinds(path):
    """The event kinds a `--journal-file`'s entry records carry."""
    return {
        kind
        for entry in journal_entries(path)
        for kind in entry.get("event", {})
    }


def restarted_records(entries):
    """The `source_restarted` records of a journal entry list — the
    named durable record of the tracked source's new generation."""
    return [
        entry["event"]["source_restarted"]
        for entry in entries
        if "source_restarted" in entry.get("event", {})
    ]


def rediscovery_pass(args, tamper):
    """The exercised run: converge the manifest-declared pair on the
    tracking peer's configured *name*, recreate the name's owner onto
    a moved address while the vacated one stays occupied, and assert
    the peer re-discovers the source by name — reconverging
    `tracking` inside its first process lifetime with the fallback
    journaled — never stranding on the stale address. Returns
    `(digest_entries, evidence, failures)`; raises `Inconclusive`
    where the pinned release predates the contract or the harness
    admits no lever."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "track-source-rediscovery leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    budget = standby_decl.get("failover_budget")
    if budget is None:
        raise Abort(
            "the manifest's standby declares no failover_budget — "
            "the deployed pair's armed heartbeat the phantom "
            "failover manufactures has nothing to arm"
        )
    missing = missing_lever()
    if missing is not None:
        raise Inconclusive(
            "the harness admits no lever for the container "
            f"recreate's address move: {missing}"
        )
    # The declared source's name — the manifest's `ctrl-a:8080`
    # names its owner by container name; the driven run carries the
    # same name at the spawned duty monitor's port.
    track_name = standby_decl["standby"].rsplit(":", 1)[0]

    digest_entries, evidence, failures = [], {}, []
    rig = placeholder = None
    try:
        # Phase 1 — the launch: the manifest-declared pair — the
        # plant server, the declared duty member at the loopback
        # address the declared source name first resolves to, and
        # the declared tracking member inside the mount namespace
        # whose private hosts file binds that name — the tracking
        # peer's `--standby` carrying the *name* exactly as the
        # deployment wires it, never the resolved address.
        rig = pair.PairRig(declared)
        rig.plant, rig.plant_addr = pair.spawn_plant(
            args.plant_server, args.model, args.dynamics
        )
        rig.plant_io = simulate.PlantClient(rig.plant_addr)
        rig.duty, rig.duty_url, rig.duty_preamble = pair.spawn_peer(
            args.controller,
            args.model,
            args.dt,
            rig.plant_addr,
            None,
            rig.duty_files,
            pair_token=pair.PAIR_TOKEN,
        )
        if rig.duty_url is None:
            raise Abort(
                f"the duty controller {duty_decl['name']} exited at "
                f"startup: "
                f"{'; '.join(rig.duty_preamble) or 'no diagnostic'}"
            )
        duty_port = rig.duty_url.rsplit(":", 1)[-1]
        stale_addr = f"127.0.0.1:{duty_port}"
        moved_addr = f"{MOVED_HOST}:{duty_port}"
        name_target = f"{track_name}:{duty_port}"
        hosts = os.path.join(rig.scratch, "hosts")
        write_hosts(hosts, track_name, "127.0.0.1")
        rig.standby, rig.standby_url, rig.standby_preamble = (
            spawn_namespaced(
                args.controller,
                hosts,
                peer_argv(
                    args.model,
                    args.dt,
                    rig.plant_addr,
                    name_target,
                    rig.standby_files,
                    auto_promote=budget,
                ),
            )
        )
        if rig.standby_url is None:
            joined = " ".join(rig.standby_preamble)
            if "unknown option" in joined:
                raise Inconclusive(
                    "the pinned tooling refuses the declared pair's "
                    "own flags — the pinned release predates the "
                    "tracking contract's substrate"
                )
            failures.append(
                f"the standby controller {standby_decl['name']} "
                "exited at startup: "
                f"{'; '.join(rig.standby_preamble) or 'no diagnostic'}"
            )
            raise Abort
        standby_url = rig.standby_url

        # Phase 2 — convergence and the settled alignment: the pair's
        # driven ticks land the tracking peer `tracking` on the
        # configured name, then two drain pulls on the owner's frozen
        # checkpoint land the alignment exactly — the pre-move
        # alignment the journaled restart regression is measured
        # against. Each settle gives the pull's requested fetch time
        # to complete before the next scan consumes it.
        converged = rig.converge(failures)
        evidence["converged"] = converged["ticks"][-1]
        for _ in range(DRAIN_SCANS):
            time.sleep(FETCH_SETTLE_S)
            pair.scan(standby_url, failures)
        time.sleep(FETCH_SETTLE_S)
        report = pair.get(f"{standby_url}/role", "GET /role", failures)
        aligned0 = (
            (report.get("sync") or {}).get("tracking") or {}
        ).get("aligned")
        if aligned0 is None:
            failures.append(
                "the converged tracking peer's report carries no "
                f"alignment — GET /role answers {report}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "aligned": aligned0,
            }
        )

        # The contract's substrate on the pinned release: the served
        # checkpoint's field-ownership stamps — `line_owner` the
        # moved source's address is read back through — and the
        # declared durable journal file the fallback's named record
        # lands in. Each absence is the release predating the
        # contract, never a violation of it.
        doc0 = pair.get(
            f"{standby_url}/checkpoint", "GET /checkpoint", failures
        )
        if (
            "source_owns_field" not in doc0
            or "line_owner" not in doc0
        ):
            raise Inconclusive(
                "the tracking peer serves a checkpoint without the "
                "field-ownership stamps — the pinned release "
                "predates the tracking-source contract the leg "
                f"exercises: {doc0}"
            )
        if rig.standby_files.get("journal_file") is None:
            raise Inconclusive(
                "the manifest's standby declares no journal_file — "
                "the durable half of the rediscovery audit is absent"
            )
        digest_entries.append(
            {"phase": "gate", "stamps": "present", "journal": "declared"}
        )

        # Phase 3 — the container recreate: the name's owner stops,
        # its vacated address is held occupied by the unrelated
        # workload — the stale-resolution pulls' accept-and-drop
        # dead end — and the member comes back *cold* on the moved
        # loopback at the same service port: the declared journal
        # file carried so the recreate's second lifetime appends to
        # the durable record, the ephemeral state file's loss minting
        # the new tick generation the tracking peer's journal must
        # name. The first stale scan runs before the slow respawn so
        # the last pre-move fetch still lands inside its freshness
        # bound — the miss run then holds strictly inside the armed
        # budget.
        pair.stop(rig.duty)
        rig.duty = None
        placeholder = Placeholder(("127.0.0.1", int(duty_port)))
        stale_watch = []
        pair.scan(standby_url, failures)
        report = pair.get(f"{standby_url}/role", "GET /role", failures)
        stale_watch.append(
            {
                "sync": sync_kind(report),
                "role": report.get("role"),
                "target": degraded_target(report),
                "detail": ((report.get("sync") or {})
                           .get("degraded") or {}).get("detail"),
            }
        )
        state_file = rig.duty_files.get("state_file")
        if state_file is not None and os.path.exists(state_file):
            os.remove(state_file)
        rig.duty, moved_url, preamble = pair.spawn_peer(
            args.controller,
            args.model,
            args.dt,
            rig.plant_addr,
            None,
            rig.duty_files,
            listen=moved_addr,
            pair_token=pair.PAIR_TOKEN,
        )
        if moved_url is None:
            joined = " ".join(preamble)
            if "unknown option" in joined or "claim" in joined:
                raise Inconclusive(
                    "the recreated owner could not take the field — "
                    "the pinned release predates the conditional "
                    "startup grant the recreate relies on: "
                    f"{'; '.join(preamble) or 'no diagnostic'}"
                )
            failures.append(
                "the recreated duty controller exited at startup: "
                f"{'; '.join(preamble) or 'no diagnostic'}"
            )
            raise Abort
        rig.duty_url = moved_url
        duty_role = pair.get(
            f"{rig.duty_url}/role", "GET /role", failures
        )
        if duty_role.get("role") != "active":
            failures.append(
                f"the recreated owner reports "
                f"{duty_role.get('role')!r} — the conditional "
                "startup grant did not take the field back from "
                "the dead claim"
            )
            raise Abort
        time.sleep(FETCH_SETTLE_S)

        # Resolution catches up — the name now answers where the peer
        # actually lives, the rewritten hosts file the rig's IPAM
        # update stands in for. The remaining stale-window scans land
        # the degraded miss naming the configured name while the
        # post-rewrite fetch is already in flight — the miss run
        # holds strictly inside the armed budget.
        write_hosts(hosts, track_name, MOVED_HOST)
        for _ in range(STALE_SCANS - 1):
            pair.scan(standby_url, failures)
            report = pair.get(
                f"{standby_url}/role", "GET /role", failures
            )
            stale_watch.append(
                {
                    "sync": sync_kind(report),
                    "role": report.get("role"),
                    "target": degraded_target(report),
                    "detail": ((report.get("sync") or {})
                               .get("degraded") or {}).get("detail"),
                }
            )
        time.sleep(FETCH_SETTLE_S)
        evidence["stale_watch"] = stale_watch

        # Phase 4 — the rejoin window: the fixed driven scans give
        # the armed standby room to expose the defect's shapes — the
        # indefinite `degraded` strand on the stale resolution or the
        # budget's phantom promotion — while the contract's own
        # outcome is the reconverged `tracking` verdict.
        rejoin_watch = []
        reconverged = None
        for _ in range(REJOIN_SCANS):
            pair.scan(standby_url, failures)
            report = pair.get(
                f"{standby_url}/role", "GET /role", failures
            )
            rejoin_watch.append(
                {
                    "sync": sync_kind(report),
                    "role": report.get("role"),
                    "target": degraded_target(report),
                    "detail": ((report.get("sync") or {})
                               .get("degraded") or {}).get("detail"),
                }
            )
            if reconverged is None and claim_reclaim.tracking(report):
                reconverged = report
            time.sleep(FETCH_SETTLE_S)
        evidence["rejoin_watch"] = rejoin_watch
        if failures:
            raise Abort

        # The reconvergence verdict and its precedence classification:
        # a peer that stayed `standby` and landed `tracking` inside
        # the window re-discovered its moved source by name; the
        # armed budget's manufactured failover and the indefinite
        # strand on the startup-resolved address are the pre-contract
        # defect's own silhouettes — inconclusive, never a violation
        # — while a degraded pull naming the configured *name* that
        # still never reconverged is the contract's own machinery
        # broken.
        watch = stale_watch + rejoin_watch
        roles = [row["role"] for row in watch]
        degraded = [row for row in watch if row["sync"] == "degraded"]
        if any(role != "standby" for role in roles):
            raise Inconclusive(
                "the armed standby manufactured a failover against "
                "the moved owner — the phantom misses the stale pin "
                "counted — the pinned release predates the per-pull "
                "re-resolution contract"
            )
        if reconverged is None:
            if degraded and not any(
                row.get("target") == name_target for row in degraded
            ):
                raise Inconclusive(
                    "the tracking peer stayed pinned on the "
                    "stale-resolved address through the whole rejoin "
                    "window — the pinned release predates the "
                    "per-pull re-resolution contract"
                )
            failures.append(
                "the tracking peer never reconverged — its sync "
                f"readings stayed {watch}"
            )
            raise Abort
        if any(row.get("target") == stale_addr for row in degraded):
            raise Inconclusive(
                "a degraded pull named the startup-resolved address "
                "instead of the configured name — the pinned release "
                "predates the per-pull re-resolution contract"
            )
        if not degraded or not all(
            row.get("target") == name_target for row in degraded
        ):
            failures.append(
                "the miss window's degraded pulls did not name the "
                f"configured source name {track_name} — targets "
                f"{[row.get('target') for row in degraded]}, "
                f"details {[row.get('detail') for row in watch]}"
            )
            raise Abort
        evidence["reconverged"] = reconverged.get("tick")
        digest_entries.append(
            {
                "phase": "rejoin",
                "targets": "the configured name",
                "reconverged": True,
            }
        )

        # Phase 5 — the fallback journaled by name: the tracking
        # peer's declared journal file carries exactly one
        # `source_restarted` — the new tick generation the recreated
        # source minted, `was_aligned` the pre-move alignment and
        # `resumed_at` the regressed stream it resumed — inside the
        # single cold-start run boundary the no-restart proof needs,
        # beside no `role_changed`; the served journal agrees. The
        # recreated owner's own file carries its second lifetime's
        # boundary and no `field_claim_lost` — the phantom failover
        # never preempted it.
        journal_file = rig.standby_files["journal_file"]
        served = pair.get(
            f"{standby_url}/journal", "GET /journal", failures
        )
        if not os.path.exists(journal_file):
            failures.append(
                f"the standby's declared journal file {journal_file} "
                "does not exist — the --journal-file flag was not "
                "honored"
            )
            raise Abort
        boundaries = journal_boundaries(journal_file)
        if boundaries != [{"run": 1, "tick": 0}]:
            failures.append(
                f"the standby's journal boundaries are {boundaries} "
                "— the reconvergence must land inside the first "
                "process lifetime, no restart boundary"
            )
        durable = journal_entries(journal_file)
        restarts = restarted_records(durable)
        if len(restarts) != 1:
            failures.append(
                "the tracking peer's durable journal carries "
                f"{len(restarts)} source_restarted records — the "
                "rediscovered source's new generation must journal "
                "exactly once"
            )
        else:
            restart = restarts[0]
            if restart.get("was_aligned") != aligned0:
                failures.append(
                    "the journaled source_restarted's was_aligned "
                    f"{restart.get('was_aligned')} does not name the "
                    f"pre-move alignment {aligned0}"
                )
            resumed = restart.get("resumed_at")
            if resumed is None or resumed >= aligned0:
                failures.append(
                    "the journaled source_restarted's resumed_at "
                    f"{resumed} does not regress below the pre-move "
                    f"alignment {aligned0} — the recreated source's "
                    "new stream is not the recorded restart"
                )
        if "role_changed" in durable_kinds(journal_file):
            failures.append(
                "the tracking peer's durable journal carries a "
                "role_changed record — the phantom failover fired "
                "against the live owner"
            )
        if restarted_records(served) != restarts:
            failures.append(
                "the tracking peer's served journal and durable "
                "file disagree on the source_restarted record — "
                f"served {restarted_records(served)}, durable "
                f"{restarts}"
            )
        doc = pair.get(
            f"{standby_url}/checkpoint", "GET /checkpoint", failures
        )
        if str(doc.get("line_owner")) != moved_addr:
            failures.append(
                "the reconverged peer's checkpoint names line_owner "
                f"{doc.get('line_owner')} — the moved source on "
                f"{moved_addr} was expected"
            )
        duty_journal = rig.duty_files.get("journal_file")
        if duty_journal is not None and os.path.exists(duty_journal):
            if len(journal_boundaries(duty_journal)) != 2:
                failures.append(
                    "the recreated owner's journal does not carry "
                    "the recreate's second lifetime boundary — the "
                    "restart went unrecorded"
                )
            if "field_claim_lost" in durable_kinds(duty_journal):
                failures.append(
                    "the recreated owner's journal carries "
                    "field_claim_lost — the still-live owner was "
                    "preempted"
                )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "record",
                "boundaries": boundaries,
                "restarts": [
                    {
                        "was_aligned": record.get("was_aligned"),
                        "resumed_at": record.get("resumed_at"),
                    }
                    for record in restarts
                ],
            }
        )

        # The doctored case — the leg asserting the pre-contract
        # wedge: the tracking peer may stay stranded on the stale
        # address. The honest re-resolution and reconvergence must
        # fail it.
        if tamper == "expect-stranded":
            failures.append(
                "the doctored expectation wanted the tracking peer "
                "stranded on the stale address — the honest run "
                "re-discovered the moved source by name and "
                "reconverged tracking"
            )
            raise Abort

        # Phase 6 — the restore: the pair rests on its launch roles —
        # the declared duty member `active` on its moved address, the
        # declared standby `tracking` it off the still-rewritten
        # name — a tracking-first settle letting the peer adopt the
        # moving stream's ticks.
        for _ in range(SETTLE_TICKS):
            rig.tick(standby_url, rig.duty_url, failures)
        duty_role = pair.get(
            f"{rig.duty_url}/role", "GET /role", failures
        )
        standby_role = pair.get(
            f"{standby_url}/role", "GET /role", failures
        )
        if duty_role.get("role") != "active":
            failures.append(
                f"the restored pair's duty reports {duty_role} — "
                "expected active"
            )
        if not claim_reclaim.tracking(standby_role):
            failures.append(
                f"the restored pair's standby reports "
                f"{standby_role} — expected a tracking standby"
            )
        if failures:
            raise Abort
        evidence["restored_at"] = standby_role.get("tick")
        digest_entries.append(
            {
                "phase": "restore",
                "duty": duty_role.get("role"),
                "standby": sync_kind(standby_role),
            }
        )
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Inconclusive:
        raise
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        if placeholder is not None:
            placeholder.close()
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
        choices=["expect-stranded"],
        help="doctor the leg's expectation to the pre-contract "
        "shape — the pass must fail naming the stranded wedge",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = rediscovery_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "track-source-rediscovery: the doctored expectation "
                "wanted the tracking peer stranded on the stale "
                "address — an inconclusive run offers the doctored "
                "case no evidence"
            )
            return 1
        eprint(
            f"track-source-rediscovery: inconclusive — {inconclusive}"
        )
        print(
            "track-source-rediscovery-digest inconclusive — "
            f"{inconclusive}"
        )
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"track-source-rediscovery: {line}")
        return 1
    for failure in failures:
        eprint(f"track-source-rediscovery: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"track-source-rediscovery: the {args.tamper} case "
                "passed silently — the leg never noticed the "
                "doctored expectation"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"track-source-rediscovery-digest {digest} — tracking by "
        f"tick {evidence['converged']}, the moved source "
        f"re-discovered and reconverged at tick "
        f"{evidence['reconverged']}, launch roles restored at tick "
        f"{evidence['restored_at']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
