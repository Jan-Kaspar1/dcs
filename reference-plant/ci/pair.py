#!/usr/bin/env python3
"""The redundant-pair leg for the reference plant — the consumer-side
proof that the standby pair `deploy/manifest.json` declares actually
runs, not just that its definition parses (WW-ENG-003, WW-LCM-001).

The `deploy` stage verifies the pair statically — the manifest's
standby wiring and persistence declarations instantiated by the rig
definition. This leg runs the declared pair on the released tooling:
`dcs-plant-server` serves the checked-in model and dynamics while the
manifest's two controllers run `dcs-controller --driven --remote`, the
tracking peer's `--standby` flag wired at the controller the manifest
names and each controller's declared `--state-file`/`--journal-file`
carried at runner-owned scratch paths — the persistence vocabulary's
first behavioral, not merely static, exercise. The run:

- converges the declared standby to `tracking`: each driven tick scans
  the tracking peer first — its `POST /scan` pulling and applying the
  owner's checkpoint — then the field owner, the peers' served
  snapshots staying identical until `GET /role` reports the standby
  `tracking` and the owner `active`;
- submits one kind-declared command — an `invoke` picked from the
  served `GET /schema` registry — receipted `accepted` on the owner
  and refused `not_active` at the standby's role boundary, settling
  `applied` identically into both peers' adopted receipt log;
- issues the documented switch — `POST /demote` on the field owner
  then `POST /promote` on the converged standby, each answered by its
  `RoleReport` receipt, beside a refused `POST /promote` on the field
  owner answering `409 already_active`;
- drives the run past the switch and asserts it continues bumplessly:
  the promoted peer settles `active` at the continuing tick, the
  demoted peer reconverges `tracking` off the address its own pulls
  announced, and the peers' images and receipt logs stay identical;
- verifies the declared persistence at runtime: each peer's state
  file holds its checkpoint at the run's final tick under the
  manifest's recorded model fingerprint, and each durable journal
  file carries the run's records — the cold-start boundary, the
  settled receipt, and its own `role_changed` transitions — with the
  entry `seq` order intact.

The leg's ordering-sensitive bring-up is the exported rig the sibling
pair-stage legs share — `launch_pair` resolves the declared pair out
of the manifest, instantiates the declared persistence under
runner-owned scratch, spawns the plant server and both released
controllers (the standby wired at the owner's monitor, or at a
dead address under `--tamper broken-peer-flag`), and returns the
`PairRig` carrying the processes, urls, persistence files, and the
plant-protocol client; the rig's `converge`, `tick`, and `switch`
carry the tracking-first driven ticks, the standby convergence, and
the documented demote/promote/handover/transition-audit switch.

Usage:

    pair.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `pair-digest <sha256>` line prints — the check runs two
passes and compares them (`pair-nondeterministic`). A contract
violation reports `pair: …` lines on stderr and exits 1 — the check's
`pair-failed`. `--tamper broken-peer-flag` wires the tracking peer's
`--standby` at an address nothing serves, so the leg proves its own
convergence assertion fires.
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
import urllib.error
import urllib.request

import simulate


def eprint(*args):
    print(*args, file=sys.stderr)


class Abort(Exception):
    """A pair-contract check failed mid-run. Carrying a message, the
    message is itself the failure; raised bare it only unwinds the run
    after the check already recorded its failures."""


# The driven ticks each phase runs — enough for the tracking peer's
# per-scan pull to converge and for the post-switch run to prove
# continuity. The actor the leg's receipted submissions declare.
CONVERGE_TICKS = 4
HANDOVER_TICKS = 4
ACTOR = "ci-pair"

# The declared pair's shared tracking secret: the announced-source
# contract is keyed-only, so both members carry the same --pair-token —
# a demoted owner's verify pull then demands the keyed `line_proof`
# only a peer holding the token stamps, and an endpoint that merely
# replays or fabricates the line's public checkpoints arms nothing. A
# real deployment's token is its own secret; the leg's fixed value only
# has to match across the pair's members.
PAIR_TOKEN = "dcs-reference-pair"


def stop(process):
    """Terminate a spawned child, escalating to kill if it lingers."""
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def closed_port():
    """An address nothing listens on — a just-released ephemeral port,
    the broken tracking target the `broken-peer-flag` tamper wires."""
    stream = socket.socket()
    stream.bind(("127.0.0.1", 0))
    address = stream.getsockname()
    stream.close()
    return f"{address[0]}:{address[1]}"


def dialable(bound):
    """The dialable form of a bound `host:port` the monitor reported:
    an unspecified bind host — the manifest's declared `0.0.0.0`
    wildcard — is dialed through loopback, the same resolution the
    serving peer applies to a wildcard `?peer=` announce."""
    host, _, port = bound.rpartition(":")
    if host == "0.0.0.0":
        return f"127.0.0.1:{port}"
    if host in ("::", "[::]"):
        return f"[::1]:{port}"
    return bound


def listen_bind(entry):
    """The controller's declared `listen` bind instantiated on a
    runner-assigned port — the manifest's wildcard bind shape
    preserved while the port stays runner-owned, the same mapping
    `persistence_files` applies to the declared paths."""
    listen = entry.get("listen")
    if not listen:
        return "127.0.0.1:0"
    return f"{listen.rsplit(':', 1)[0]}:0"


def spawn_peer(
    controller,
    model,
    dt,
    plant_addr,
    standby,
    files,
    auto_promote=None,
    listen="127.0.0.1:0",
    bound=None,
    pair_token=None,
):
    """Spawn `dcs-controller <model> --remote … --driven` for one pair
    peer — `standby` the manifest's tracking wiring (None on the field
    owner), `auto_promote` the manifest's declared failover budget
    arming that standby's self-promotion (None leaves promotion
    manual), `files` the controller's declared persistence paths under
    the leg's scratch directory, `listen` the `--listen` bind (the
    declared wildcard host on a runner port under a declared-binds
    launch, the default an ephemeral loopback bind), `bound` an
    optional list the verbatim bound address is appended to — the
    leg's evidence the declared wildcard bind actually deployed —
    `pair_token` the deployment's shared tracking secret a pair member
    carries (None leaves the run unkeyed — the foreign and lone peers
    the negotiation and startup legs spawn). Returns `(process,
    monitor_url, preamble)`: `monitor_url` is the
    dialable form of the reported bind — a wildcard bind normalized
    to loopback — or None when the process exits before reporting a
    listener, the preamble then carrying the startup refusal's stderr
    lines."""
    argv = [
        controller,
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
    if pair_token is not None:
        argv += ["--pair-token", pair_token]
    for field, flag in (
        ("state_file", "--state-file"),
        ("journal_file", "--journal-file"),
    ):
        if files.get(field) is not None:
            argv += [flag, files[field]]
    process = subprocess.Popen(argv, stderr=subprocess.PIPE, text=True)
    preamble = []
    for line in process.stderr:
        line = line.strip()
        if "listening on" in line:
            raw = line.rsplit(None, 1)[-1]
            if bound is not None:
                bound.append(raw)
            return process, "http://" + dialable(raw), preamble
        preamble.append(line)
    process.wait(timeout=10)
    return process, None, preamble


def request(url, body):
    """POST `body` and return `(status, decoded)` — the status kept so
    refused requests assert their named answers rather than raising."""
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return error.code, raw.decode(errors="replace")


def get(url, what, failures):
    """GET a JSON endpoint, recording a failure and unwinding on any
    transport error."""
    try:
        return simulate.http(url)
    except Exception as error:
        failures.append(f"{what} answered {error}")
        raise Abort


def scan(url, failures):
    """Drive one scan through `POST /scan`; returns the served
    snapshot."""
    try:
        return simulate.http(f"{url}/scan", {"scans": 1})
    except Exception as error:
        failures.append(f"POST /scan on {url} answered {error}")
        raise Abort


def select_snapshot(snapshot):
    """The run-state sections two converged peers must serve
    identically — the field image plus the checkpoint-carried force
    set and parameters. The monitor-stamped `publication` section is
    monitor-local and excluded."""
    return {
        key: snapshot.get(key) for key in ("tick", "points", "forces", "parameters")
    }


def journal_records(path):
    """The `--journal-file`'s lines in file order: `("boundary",
    {"run", "tick"})` markers and `("entry", entry)` records. A
    `tracking_source_adopted` entry names the monitor address the
    demotion verified — carrying the run's ephemeral listen port, the
    one detail two identical passes cannot share — so the returned
    entry holds the address's host with the port elided, keeping the
    record digest-stable without hiding which host was adopted."""
    records = []
    with open(path) as handle:
        for number, line in enumerate(handle, 1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise Abort(
                    f"journal file {path} line {number} does not parse: {error}"
                )
            if "run_boundary" in record:
                records.append(("boundary", record["run_boundary"]))
            elif "entry" in record:
                entry = record["entry"]
                adopted = entry.get("event", {}).get(
                    "tracking_source_adopted"
                )
                if adopted is not None:
                    adopted["source"] = adopted["source"].rsplit(":", 1)[0]
                records.append(("entry", entry))
            else:
                raise Abort(
                    f"journal file {path} line {number} is not a journal record"
                )
    return records


def role_transitions(entries):
    """The `role_changed` stream of a journal entry list —
    `(tick, from, to)` per entry, in `seq` order."""
    return [
        (entry["tick"], change["from"], change["to"])
        for entry in entries
        if "role_changed" in entry.get("event", {})
        for change in [entry["event"]["role_changed"]]
    ]


def manifest_pair(path):
    """The declared pair from the deployment manifest: the standby
    entry — the one carrying `standby`, whose `<name>:<port>` value
    names the field owner it follows — plus that named duty entry.
    Returns `(manifest, duty, standby)`, or None when the manifest
    declares no such pair."""
    with open(path) as handle:
        manifest = json.load(handle)
    controllers = manifest.get("controllers", [])
    by_name = {entry["name"]: entry for entry in controllers}
    standbys = [entry for entry in controllers if "standby" in entry]
    if len(standbys) != 1:
        return None
    standby = standbys[0]
    duty = by_name.get(standby["standby"].rsplit(":", 1)[0])
    if duty is None or "standby" in duty:
        return None
    return manifest, duty, standby


def declared_command(schema):
    """One `(component, spec)` the served registry declares natively —
    the `invoke`-addressed command the leg submits — or None when the
    registry carries none."""
    commands = simulate.declared_commands(schema)
    return commands[0] if commands else None


def spawn_plant(plant_server, model, dynamics):
    """Spawn `dcs-plant-server <model> --dynamics <doc>` listening on
    an ephemeral loopback address — the simulated plant the rig's
    controllers remote against. Returns `(process, address)`."""
    plant = subprocess.Popen(
        [
            plant_server,
            model,
            "--dynamics",
            dynamics,
            "--listen",
            "127.0.0.1:0",
        ],
        stderr=subprocess.PIPE,
        text=True,
    )
    return plant, simulate.listen_address(plant, "dcs-plant-server")


def persistence_files(scratch, entry):
    """The controller's declared persistence file basenames
    instantiated under the rig's runner-owned scratch directory — the
    manifest's container paths become per-peer files."""
    root = os.path.join(scratch, entry["name"])
    os.makedirs(root, exist_ok=True)
    return {
        field: os.path.join(root, os.path.basename(entry[field]))
        if entry.get(field)
        else None
        for field in ("state_file", "journal_file")
    }


def scan_pair(tracked_url, owner_url, failures):
    """The tracking-first scan ordering one driven pair tick needs —
    the tracking peer's `POST /scan` pulling and applying the owner's
    latest checkpoint, then the field owner's. Returns `(tracked,
    owner)` — both served snapshots."""
    return scan(tracked_url, failures), scan(owner_url, failures)


def tick(tracked_url, owner_url, failures, diverged=None):
    """One driven pair tick — `scan_pair`'s ordering plus the
    identical-image assertion. A one-tick lag the next pull heals is
    the carried-command shape (issue #689): the tracker's quiesced
    scan carries an adopted receipt instead of settling it, so its
    image trails the owner's by the command's effect until the
    following pull adopts the settlement. The tick absorbs exactly
    that: on a mismatch it runs one more tracking-first pair tick
    and requires convergence there — anything still diverged aborts
    with the recorded wording. Returns `(tracked, owner)`."""
    tracked, owner = scan_pair(tracked_url, owner_url, failures)
    if select_snapshot(tracked) != select_snapshot(owner):
        tracked, owner = scan_pair(tracked_url, owner_url, failures)
        if select_snapshot(tracked) != select_snapshot(owner):
            failures.append(
                (
                    diverged
                    or "the tracking peer's image diverged from the field "
                    "owner's at tick {tick}"
                ).format(tick=owner["tick"])
            )
            raise Abort
    return tracked, owner


class PairRig:
    """The launched manifest-declared pair — the rig the pair-stage
    legs share: the spawned plant server and both released
    controllers, their monitor urls and startup preambles, the
    declared persistence files under the rig's runner-owned scratch,
    and the plant-protocol client. `close` stops the children and
    removes the scratch root."""

    def __init__(self, declared):
        self.manifest, self.duty_decl, self.standby_decl = declared
        self.fingerprint = int(self.manifest["model"]["fingerprint"], 16)
        self.scratch = tempfile.mkdtemp(prefix="dcs-pair-")
        self.duty_files = persistence_files(self.scratch, self.duty_decl)
        self.standby_files = persistence_files(self.scratch, self.standby_decl)
        self.plant = self.duty = self.standby = None
        self.plant_addr = None
        self.plant_io = None
        self.duty_url = self.standby_url = None
        self.duty_preamble = self.standby_preamble = []
        # The verbatim `host:port` each monitor reported binding —
        # under a declared-binds launch the wildcard evidence the
        # demote-reconvergence leg asserts.
        self.duty_bound = self.standby_bound = []

    def close(self):
        """Stop the spawned children — tracking peer, field owner,
        plant — close the plant client, and remove the scratch root."""
        if self.plant_io is not None:
            self.plant_io.close()
        stop(self.standby)
        stop(self.duty)
        stop(self.plant)
        shutil.rmtree(self.scratch, ignore_errors=True)

    def peer_name(self, url):
        """The manifest name of the peer serving `url`."""
        return {
            self.duty_url: self.duty_decl["name"],
            self.standby_url: self.standby_decl["name"],
        }[url]

    def tick(self, tracked_url, owner_url, failures, diverged=None):
        """One driven pair tick — the module `tick` on the rig's
        peers."""
        return tick(tracked_url, owner_url, failures, diverged)

    def converge(self, failures, count=CONVERGE_TICKS):
        """The standby-convergence phase: `count` driven ticks scanning
        the tracking peer first so each pull applies the owner's
        latest checkpoint and the peers rest at the same tick with
        identical images — then the role reports, the standby expected
        `tracking` and the field owner `active`. Returns the converge
        record: `ticks`, the final `owner` snapshot, and both role
        reports."""
        ticks = []
        for _ in range(count):
            _tracked, owner = self.tick(
                self.standby_url, self.duty_url, failures
            )
            ticks.append(owner["tick"])
        standby_role = get(f"{self.standby_url}/role", "GET /role", failures)
        duty_role = get(f"{self.duty_url}/role", "GET /role", failures)
        sync = standby_role.get("sync")
        if standby_role.get("role") != "standby" or not (
            isinstance(sync, dict) and "tracking" in sync
        ):
            failures.append(
                "the tracking peer never reported tracking — GET /role "
                f"answers {standby_role}"
            )
            raise Abort
        if duty_role.get("role") != "active":
            failures.append(
                f"the field owner reports {duty_role.get('role')!r}, "
                "expected active"
            )
            raise Abort
        return {
            "ticks": ticks,
            "owner": owner,
            "duty_role": duty_role,
            "standby_role": standby_role,
        }

    def served_transitions(self, url, failures):
        """The `role_changed` stream the peer's served journal carries
        — `(tick, from, to)` per entry, in `seq` order."""
        journal = get(f"{url}/journal", "GET /journal", failures)
        return role_transitions(journal)

    def demote(self, url, failures, what="the field owner"):
        """`POST /demote` asserting the answered RoleReport carries
        `demoting`; returns the report. `what` names the peer in the
        failure line."""
        status, report = request(f"{url}/demote", {})
        if status != 200 or report.get("role") != "demoting":
            failures.append(
                f"POST /demote on {what} answered {status} {report}, "
                "expected a demoting report"
            )
            raise Abort
        return report

    def promote(self, url, failures, what="the converged standby", note=""):
        """`POST /promote` asserting the answered RoleReport carries
        `promoting`; returns the report. `note` appends the caller's
        clause to the expectation wording."""
        status, report = request(f"{url}/promote", {})
        if status != 200 or report.get("role") != "promoting":
            failures.append(
                f"POST /promote on {what} answered {status} {report}, "
                f"expected a promoting report{note}"
            )
            raise Abort
        return report

    def switch(
        self,
        demote_url,
        promote_url,
        failures,
        handover=HANDOVER_TICKS,
        demote_what="the field owner",
        promote_what="the converged standby",
        promote_note="",
        audit_receipts=False,
        after_promote=None,
        after_tick=None,
    ):
        """The documented switch: `POST /demote` on the field owner
        then `POST /promote` on the converged peer — each answered by
        its RoleReport — then `handover` driven ticks scanning the
        demoted peer first, asserting the run continues bumplessly:
        identical images, the promoted peer settling `active`, the
        demoted peer reconverged `tracking`, each peer's served
        journal audited for the switch's role transitions appended to
        its pre-switch record, and — with `audit_receipts` — the
        adopted receipt logs asserted one identical log. `after_promote`,
        when given, runs once after the promote report and before the
        first handover tick — the last moment a leg can act on the
        demoted peer before its tracking pulls begin. `after_tick`,
        when given, runs after each handover tick — the window a leg
        audits the pair's served tracking state across the pull
        train. Returns the switch record: the demote/promote reports,
        the handover `ticks` and final `owner` snapshot, both role
        reports, the promoted peer's receipts when audited, and each
        named peer's served transitions."""
        pre = {
            url: self.served_transitions(url, failures)
            for url in (demote_url, promote_url)
        }
        record = {
            "demote": self.demote(demote_url, failures, demote_what),
            "promote": self.promote(
                promote_url, failures, promote_what, promote_note
            ),
            "ticks": [],
            "owner": None,
        }
        if after_promote is not None:
            after_promote()
        for _ in range(handover):
            _tracked, owner = self.tick(
                demote_url,
                promote_url,
                failures,
                diverged="the demoted peer's image diverged from the "
                "promoted owner's at tick {tick} — the switch was not "
                "bumpless",
            )
            record["ticks"].append(owner["tick"])
            record["owner"] = owner
            if after_tick is not None:
                after_tick()
        if not handover:
            return record
        demoted_role = get(f"{demote_url}/role", "GET /role", failures)
        promoted_role = get(f"{promote_url}/role", "GET /role", failures)
        if promoted_role.get("role") != "active":
            failures.append(
                f"the promoted peer reports {promoted_role.get('role')!r}, "
                "expected active"
            )
            raise Abort
        sync = demoted_role.get("sync")
        if demoted_role.get("role") != "standby" or not (
            isinstance(sync, dict) and "tracking" in sync
        ):
            failures.append(
                "the demoted peer never reconverged — GET /role "
                f"answers {demoted_role}"
            )
            raise Abort
        record["demoted_role"] = demoted_role
        record["promoted_role"] = promoted_role
        if audit_receipts:
            receipts_demoted = get(
                f"{demote_url}/receipts", "GET /receipts", failures
            )
            receipts_promoted = get(
                f"{promote_url}/receipts", "GET /receipts", failures
            )
            if receipts_demoted != receipts_promoted:
                failures.append(
                    "the peers' receipt logs diverged across the "
                    "switch — the adopted audit is not one log"
                )
                raise Abort
            record["receipts"] = receipts_promoted
        expected = {
            demote_url: [("active", "demoting"), ("demoting", "standby")],
            promote_url: [
                ("standby", "promoting"),
                ("promoting", "active"),
            ],
        }
        transitions = {}
        for url in (demote_url, promote_url):
            got = self.served_transitions(url, failures)
            want = [
                (frm, to) for _tick, frm, to in pre[url]
            ] + expected[url]
            if [(frm, to) for _tick, frm, to in got] != want:
                failures.append(
                    f"{self.peer_name(url)}'s served journal carries "
                    f"the role transitions {got}, expected {want}"
                )
            transitions[self.peer_name(url)] = got
        if failures:
            raise Abort
        record["transitions"] = transitions
        return record


def launch_pair(args, manifest, tamper=None, auto_promote=None,
                declared_binds=False):
    """Resolve the declared standby pair and launch it on the released
    tooling — the bring-up the pair-stage legs share: each
    controller's declared persistence instantiated under a
    runner-owned scratch directory, `dcs-plant-server` serving the
    model and dynamics, then the two released `dcs-controller
    --driven --remote` peers — the field owner first, then the
    standby wired at the owner's monitor (or, under the
    `broken-peer-flag` tamper, at an address nothing serves), both
    keyed on the leg's shared `PAIR_TOKEN` so the announced-source
    demotion contract the switch legs exercise runs attested — each
    spawn's startup refusal reported through `Abort`. `manifest` is
    the manifest path or `manifest_pair`'s resolved `(manifest, duty,
    standby)` — callers resolving it themselves keep their own
    no-pair wording. `auto_promote`, when given, arms the spawned
    standby's `--auto-promote` flag — the manifest's declared
    `failover_budget` carried to the invocation. `declared_binds`,
    when true, binds each controller's `--listen` on its declared
    listen's host with a runner-assigned port — the manifest's
    wildcard bind shape deployed, each verbatim bound address
    recorded on the rig's `duty_bound`/`standby_bound`. Returns the
    PairRig."""
    declared = (
        manifest_pair(manifest)
        if isinstance(manifest, (str, os.PathLike))
        else manifest
    )
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the pair rig has "
            "nothing to exercise"
        )
    rig = PairRig(declared)
    try:
        rig.plant, rig.plant_addr = spawn_plant(
            args.plant_server, args.model, args.dynamics
        )
        rig.plant_io = simulate.PlantClient(rig.plant_addr)
        rig.duty, rig.duty_url, rig.duty_preamble = spawn_peer(
            args.controller,
            args.model,
            args.dt,
            rig.plant_addr,
            None,
            rig.duty_files,
            listen=listen_bind(rig.duty_decl) if declared_binds else "127.0.0.1:0",
            bound=rig.duty_bound,
            pair_token=PAIR_TOKEN,
        )
        if rig.duty_url is None:
            raise Abort(
                f"the duty controller {rig.duty_decl['name']} exited at "
                f"startup: "
                f"{'; '.join(rig.duty_preamble) or 'no diagnostic'}"
            )
        # The manifest's standby wiring names the peer by its
        # deployment address; the driven run wires the tracking peer at
        # the spawned duty monitor — or, under the broken-peer-flag
        # tamper, at an address nothing serves.
        target = rig.duty_url.removeprefix("http://")
        if tamper == "broken-peer-flag":
            target = closed_port()
        rig.standby, rig.standby_url, rig.standby_preamble = spawn_peer(
            args.controller,
            args.model,
            args.dt,
            rig.plant_addr,
            target,
            rig.standby_files,
            auto_promote=auto_promote,
            listen=listen_bind(rig.standby_decl) if declared_binds else "127.0.0.1:0",
            bound=rig.standby_bound,
            pair_token=PAIR_TOKEN,
        )
        if rig.standby_url is None:
            raise Abort(
                f"the standby controller {rig.standby_decl['name']} "
                f"exited at startup: "
                f"{'; '.join(rig.standby_preamble) or 'no diagnostic'}"
            )
    except Exception:
        rig.close()
        raise
    return rig


def pair_pass(args, tamper):
    """The pair run: converge, receipt, switch, continue, persist.
    Returns `(digest_entries, evidence, failures)`."""
    declared = manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the pair leg has "
            "nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = launch_pair(args, declared, tamper)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        duty_files, standby_files = rig.duty_files, rig.standby_files

        # Phase 1 — convergence. Each driven tick scans the tracking
        # peer first — its request pulling and applying the owner's
        # latest checkpoint — then the field owner, so the peers rest
        # at the same tick with identical images.
        converged = rig.converge(failures)
        evidence["converged"] = converged["ticks"][-1]
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )

        # Phase 2 — a kind-declared command receipted on the owner:
        # one `invoke` the served registry declares, submitted through
        # `POST /command`'s bounded path — and refused `not_active` at
        # the standby's role boundary, the pair's command admission
        # contract.
        schema = get(f"{duty_url}/schema", "GET /schema", failures)
        declared = declared_command(schema)
        if declared is None:
            failures.append(
                "the served registry declares no command — the pair's "
                "receipted-command leg has nothing to exercise"
            )
            raise Abort
        component, spec = declared
        command = {
            "invoke": {
                "component": component,
                "command": spec["name"],
                "arguments": simulate.command_arguments(spec),
            }
        }
        status, receipt = request(
            f"{duty_url}/command", {"command": command, "actor": ACTOR}
        )
        if status != 200 or simulate.receipt_outcome(receipt) != "accepted":
            failures.append(
                f"the declared command {spec['name']} on {component} "
                f"answered {status} {receipt}, expected an accepted receipt"
            )
            raise Abort
        status, refusal = request(
            f"{standby_url}/command", {"command": command, "actor": ACTOR}
        )
        reason = (
            refusal.get("outcome", {}).get("rejected", {}).get("reason", {})
            if isinstance(refusal, dict)
            else {}
        )
        if status != 200 or "not_active" not in reason:
            failures.append(
                f"the standby's role boundary answered {status} {refusal}, "
                "expected a rejected not_active receipt"
            )
            raise Abort
        # The settling tick: the duty applies the admitted invoke at
        # its scan boundary while the tracker carries its adopted
        # receipt (issue #689) — a quiesced scan must not mint an
        # `Applied` the line never ordered. `tick` absorbs exactly
        # that one-tick lag and returns the reconverged pair, so the
        # adopted audit reads as one log below. A field-moving invoke
        # additionally names one cycle of divergence (staged
        # pre-command outputs against the commanded field); bounded
        # extra cycles heal the verdict before the switch reads
        # convergence.
        _tracked, owner = tick(standby_url, duty_url, failures)
        for _ in range(5):
            standby_role = get(f"{standby_url}/role", "GET /role", failures)
            sync = standby_role.get("sync")
            if isinstance(sync, dict) and "tracking" in sync:
                break
            scan(duty_url, failures)
            _tracked, owner = tick(standby_url, duty_url, failures)
        else:
            failures.append(
                "the tracking peer never reconverged after the "
                "command's settling tick"
            )
            raise Abort
        receipts_duty = get(f"{duty_url}/receipts", "GET /receipts", failures)
        receipts_standby = get(
            f"{standby_url}/receipts", "GET /receipts", failures
        )
        if receipts_duty != receipts_standby:
            failures.append(
                "the peers' receipt logs diverged — the adopted audit "
                "is not one log"
            )
            raise Abort
        settled = [
            entry
            for entry in receipts_duty
            if entry.get("command") == command
            and simulate.receipt_outcome(entry) == "applied"
        ]
        if not settled:
            failures.append(
                f"the declared command {spec['name']} on {component} "
                "never settled applied into the adopted receipt log"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "command",
                "component": component,
                "command": spec["name"],
                "receipt": receipt,
                "refusal": refusal,
                "receipts": receipts_duty,
            }
        )

        # Phase 3 — the receipted switch: a promote on the field owner
        # is the named refusal first, then the documented order —
        # demote the owner, promote the converged standby — each
        # answered by its RoleReport.
        status, refused = request(f"{duty_url}/promote", {})
        if status != 409 or refused != "already_active":
            failures.append(
                f"POST /promote on the field owner answered {status} "
                f"{refused}, expected 409 already_active"
            )
            raise Abort
        # Phase 4 — the run continues bumplessly: the demoted peer
        # tracks the new owner off the address its own pulls
        # announced, each driven tick keeping the peers identical, the
        # promoted peer serving active at the continuing tick.
        switched = rig.switch(
            duty_url, standby_url, failures, audit_receipts=True
        )
        evidence["switched_at"] = switched["demote"]["tick"]
        digest_entries.append(
            {
                "phase": "switch",
                "refused_promote": refused,
                "demote": switched["demote"],
                "promote": switched["promote"],
            }
        )
        owner = switched["owner"]
        digest_entries.append(
            {
                "phase": "handover",
                "ticks": switched["ticks"],
                "duty_role": switched["demoted_role"],
                "standby_role": switched["promoted_role"],
                "receipts": switched["receipts"],
            }
        )

        # Phase 5 — the record: each peer's served journal carries its
        # own role transitions beside the settled receipt, and the
        # declared persistence paths hold the run's durable state —
        # the journal files' boundaries and `seq` order intact, the
        # state files checkpointed at the run's final tick under the
        # manifest's model fingerprint.
        final_tick = owner["tick"]
        expected = {
            duty_decl["name"]: [
                ("active", "demoting"),
                ("demoting", "standby"),
            ],
            standby_decl["name"]: [
                ("standby", "promoting"),
                ("promoting", "active"),
            ],
        }
        files = {
            duty_decl["name"]: duty_files,
            standby_decl["name"]: standby_files,
        }
        persisted = {}
        for name in (duty_decl["name"], standby_decl["name"]):
            transitions = switched["transitions"][name]
            want = expected[name]
            if [(frm, to) for _tick, frm, to in transitions] != want:
                failures.append(
                    f"{name}'s served journal carries the role "
                    f"transitions {transitions}, expected {want}"
                )
            journal_path = files[name].get("journal_file")
            state_path = files[name].get("state_file")
            record = {"served_transitions": transitions}
            if journal_path is not None:
                if not os.path.exists(journal_path):
                    failures.append(
                        f"{name}'s declared journal file {journal_path} "
                        "does not exist — the --journal-file flag was "
                        "not honored"
                    )
                else:
                    records = journal_records(journal_path)
                    boundaries = [
                        record_
                        for kind, record_ in records
                        if kind == "boundary"
                    ]
                    if boundaries != [{"run": 1, "tick": 0}]:
                        failures.append(
                            f"{name}'s journal boundaries are {boundaries}, "
                            "expected the single cold-start marker"
                        )
                    entries = [
                        record_
                        for kind, record_ in records
                        if kind == "entry"
                    ]
                    seqs = [entry["seq"] for entry in entries]
                    if seqs != list(range(1, len(seqs) + 1)):
                        failures.append(
                            f"{name}'s journal seqs are not 1..n in order: {seqs}"
                        )
                    file_transitions = role_transitions(entries)
                    if [(frm, to) for _tick, frm, to in file_transitions] != want:
                        failures.append(
                            f"{name}'s journal file carries the role "
                            f"transitions {file_transitions}, expected {want}"
                        )
                    if not any(
                        "command_settled" in entry.get("event", {})
                        for entry in entries
                    ):
                        failures.append(
                            f"{name}'s journal file carries no settled "
                            "receipt — the run's command record is lost"
                        )
                    record["journal_records"] = records
            if state_path is not None:
                if not os.path.exists(state_path):
                    failures.append(
                        f"{name}'s declared state file {state_path} does "
                        "not exist — the --state-file flag was not honored"
                    )
                else:
                    try:
                        with open(state_path) as handle:
                            checkpoint = json.load(handle)
                    except (OSError, json.JSONDecodeError) as error:
                        failures.append(
                            f"{name}'s state file does not parse: {error}"
                        )
                        checkpoint = None
                    if checkpoint is not None:
                        if checkpoint.get("tick") != final_tick:
                            failures.append(
                                f"{name}'s state file persisted tick "
                                f"{checkpoint.get('tick')} while the run "
                                f"stood at {final_tick}"
                            )
                        if (
                            checkpoint.get("model_fingerprint")
                            != rig.fingerprint
                        ):
                            failures.append(
                                f"{name}'s state file carries fingerprint "
                                f"{checkpoint.get('model_fingerprint')}, the "
                                f"manifest declares {rig.fingerprint}"
                            )
                        record["state_tick"] = checkpoint.get("tick")
            persisted[name] = record
        digest_entries.append({"phase": "record", "persisted": persisted})
        evidence["final_tick"] = final_tick
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
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
        choices=["broken-peer-flag"],
        help="doctor the pair's wiring — the pass must fail naming "
        "the evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = pair_pass(args, args.tamper)
    except Abort as abort:
        for line in abort.args:
            eprint(f"pair: {line}")
        return 1
    for failure in failures:
        eprint(f"pair: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"pair: the {args.tamper} case passed silently — the leg "
                "never noticed the broken wiring"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    records = digest_entries[-1]["persisted"]
    counts = "+".join(
        str(len(record.get("journal_records", [])))
        for record in records.values()
    )
    print(
        f"pair-digest {digest} — tracking by tick "
        f"{evidence['converged']}, switched at tick "
        f"{evidence['switched_at']}, run continued to tick "
        f"{evidence['final_tick']}, {counts} persisted journal records"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
