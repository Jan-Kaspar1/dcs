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

With `--parity` the script runs the emit-identical leg instead
(decision 84, WW-FND-003): the declared pair converges the same way,
then the served registry's first kind-declared event names the emitter
— the emitted model's `sequencer` and its `step_completed` — whose
writable boolean `run` input is held through the field owner's
receipted `POST /command` path while the same write at the standby's
role boundary answers the named `not_active` refusal. The apply tick
scans the owner first — a field-carried write is one the quiesced
peer cannot issue, so the standby's next pull must land the
post-application checkpoint — and the remaining driven scans pace
standby-first, each `POST /scan` pulling and applying the owner's
checkpoint before the tracking peer scans. The declared step table
runs out until the counted emission set stands, and both peers'
`GET /resources` views must collect the same routed `event_emitted`
records: identical outer and inner component attribution, declared
event identities, ordered payload fields, tick, and retention — the
stream-local `seq` positions excluded, each peer's store numbering its
own — while the standby still reports `tracking`. A divergence, a
dropped record, or a hollow stream reports `event-parity-failed` on
stderr and exits 1; a clean pass prints one `event-parity-digest
<sha256>` line the check compares across two passes
(`event-parity-nondeterministic`).

Usage:

    pair.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json \
        [--parity]

On success one `pair-digest <sha256>` line prints — the check runs two
passes and compares them (`pair-nondeterministic`). A contract
violation reports `pair: …` lines on stderr and exits 1 — the check's
`pair-failed`. `--tamper broken-peer-flag` wires the tracking peer's
`--standby` at an address nothing serves, so the leg proves its own
convergence assertion fires. Under `--parity` the tamper choices
`dropped-event-record` and `reattributed-event-record` doctor the
standby's served resource view — a missing emission record and a
re-attributed one — so the leg proves its parity assertion fires:
each must report `event-parity-failed`.
"""

import argparse
import hashlib
import json
import os
import re
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


def spawn_peer(controller, model, dt, plant_addr, standby, files):
    """Spawn `dcs-controller <model> --remote … --driven` for one pair
    peer — `standby` the manifest's tracking wiring (None on the field
    owner), `files` the controller's declared persistence paths under
    the leg's scratch directory. Returns `(process, monitor_url,
    preamble)`: `monitor_url` is None when the process exits before
    reporting a listener — the preamble then carries the startup
    refusal's stderr lines."""
    argv = [
        controller,
        model,
        "--remote",
        plant_addr,
        "--driven",
        "--listen",
        "127.0.0.1:0",
        "--dt",
        str(dt),
    ]
    if standby is not None:
        argv += ["--standby", standby]
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
            return process, "http://" + line.rsplit(None, 1)[-1], preamble
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
    {"run", "tick"})` markers and `("entry", entry)` records."""
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
                records.append(("entry", record["entry"]))
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


def persistence_files(scratch, entry):
    """The controller's declared persistence file basenames
    instantiated under the leg's runner-owned scratch directory — the
    manifest's container paths become per-peer files."""
    root = os.path.join(scratch, entry["name"])
    os.makedirs(root, exist_ok=True)
    return {
        field: os.path.join(root, os.path.basename(entry[field]))
        if entry.get(field)
        else None
        for field in ("state_file", "journal_file")
    }


def spawn_rig(args, duty_decl, standby_decl, duty_files, standby_files, tamper):
    """Spawn the manifest-declared rig: `dcs-plant-server` on the
    checked-in model and dynamics, then the two controllers as
    released `--driven --remote` instances — the tracking peer's
    `--standby` at the spawned duty monitor (or, under the
    broken-peer-flag tamper, at an address nothing serves). Returns
    `(plant, duty, duty_url, standby, standby_url)` with the spawned
    processes held for the caller's cleanup."""
    plant = subprocess.Popen(
        [
            args.plant_server,
            args.model,
            "--dynamics",
            args.dynamics,
            "--listen",
            "127.0.0.1:0",
        ],
        stderr=subprocess.PIPE,
        text=True,
    )
    plant_addr = simulate.listen_address(plant, "dcs-plant-server")
    duty, duty_url, preamble = spawn_peer(
        args.controller, args.model, args.dt, plant_addr, None, duty_files
    )
    if duty_url is None:
        raise Abort(
            f"the duty controller {duty_decl['name']} exited at "
            f"startup: {'; '.join(preamble) or 'no diagnostic'}"
        )
    # The manifest's standby wiring names the peer by its deployment
    # address; the driven run wires the tracking peer at the spawned
    # duty monitor — or, under the broken-peer-flag tamper, at an
    # address nothing serves.
    target = duty_url.removeprefix("http://")
    if tamper == "broken-peer-flag":
        target = closed_port()
    standby, standby_url, preamble = spawn_peer(
        args.controller,
        args.model,
        args.dt,
        plant_addr,
        target,
        standby_files,
    )
    if standby_url is None:
        raise Abort(
            f"the standby controller {standby_decl['name']} exited at "
            f"startup: {'; '.join(preamble) or 'no diagnostic'}"
        )
    return plant, duty, duty_url, standby, standby_url


def converge_to_tracking(duty_url, standby_url, failures):
    """The convergence phase both legs share: each driven tick scans
    the tracking peer first — its `POST /scan` pulling and applying
    the owner's checkpoint — then the field owner, so the peers rest
    at the same tick with identical images until `GET /role` reports
    the standby `tracking` and the owner `active`. Returns `(ticks,
    standby_role, duty_role)`."""
    ticks = []
    for _ in range(CONVERGE_TICKS):
        tracked = scan(standby_url, failures)
        owner = scan(duty_url, failures)
        if select_snapshot(tracked) != select_snapshot(owner):
            failures.append(
                "the tracking peer's image diverged from the field "
                f"owner's at tick {owner['tick']}"
            )
            raise Abort
        ticks.append(owner["tick"])
    standby_role = get(f"{standby_url}/role", "GET /role", failures)
    duty_role = get(f"{duty_url}/role", "GET /role", failures)
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
    return ticks, standby_role, duty_role


def event_records(view):
    """The routed `event_emitted` records a served `GET /resources`
    view carries — every component's `events` in served order,
    projected to the comparable record: the outer resource `name` the
    emission attributes under as `component`, `tick` and `retention`
    carried, and the full inner EmittedEvent with `fields` the ordered
    item list. The entry's stream-local `seq` and the view's
    `publication` marker are excluded — local stream positions, not
    routed identity."""
    records = []
    for component in view.get("components", []):
        for entry in component.get("events", []):
            emitted = (
                entry.get("event", {}).get("event_emitted", {}).get("event")
            )
            if not isinstance(emitted, dict):
                continue
            records.append(
                {
                    "component": component.get("name"),
                    "tick": entry.get("tick"),
                    "retention": entry.get("retention", "journal"),
                    "event": dict(
                        emitted,
                        fields=list(emitted.get("fields", {}).items()),
                    ),
                }
            )
    return records


def record_divergence(records, expected):
    """The first difference between a peer's collected records and the
    expected set — the mismatched record's served and expected forms,
    or the first expected record a shorter stream dropped."""
    for index, (got, want) in enumerate(zip(records, expected)):
        if got != want:
            return (
                f"record {index} serves "
                f"{json.dumps(got, sort_keys=True, default=str)}, "
                f"expected {json.dumps(want, sort_keys=True, default=str)}"
            )
    if len(records) < len(expected):
        tail = json.dumps(expected[len(records):], sort_keys=True, default=str)
        return (
            f"{len(records)} records against the expected "
            f"{len(expected)} — missing {tail}"
        )
    if len(records) > len(expected):
        tail = json.dumps(records[len(expected):], sort_keys=True, default=str)
        return (
            f"{len(records)} records against the expected "
            f"{len(expected)} — extra {tail}"
        )
    return "the streams differ"


def assert_event_parity(active_view, standby_view, expected):
    """Decision 84's emit-identical rule at the served surface: both
    peers' collected `event_emitted` records must equal the expected
    counted set — identical component attribution, declared
    identities, ordered fields, tick, and retention, the stream-local
    seqs excluded. A divergence raises Abort carrying
    `event-parity-failed` and the first difference."""
    for label, view in (("active", active_view), ("standby", standby_view)):
        records = event_records(view)
        if records != expected:
            raise Abort(
                f"event-parity-failed: the {label} peer's served "
                f"emissions diverge — {record_divergence(records, expected)}"
            )
    return expected


def doctor_view(view, tamper):
    """Apply a parity tamper to a fetched served resource view — the
    doctored standby whose missing or re-attributed emission record
    the assertion must catch."""
    for component in view.get("components", []):
        events = component.get("events", [])
        for index, entry in enumerate(events):
            if "event_emitted" not in entry.get("event", {}):
                continue
            if tamper == "dropped-event-record":
                del events[index]
            elif tamper == "reattributed-event-record":
                entry["event"]["event_emitted"]["event"]["component"] = (
                    "doctored:0"
                )
            return


def parity_pass(args, tamper):
    """The emit-identical leg: converge the declared standby to
    tracking, drive the emitted model's sequencer through the field
    owner's receipted path until the counted `step_completed` set
    stands, and assert both peers serve the same routed event
    records. Returns `(digest_entries, evidence, failures)`."""
    declared = manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the event-parity "
            "leg has nothing to exercise"
        )
    manifest, duty_decl, standby_decl = declared
    with open(args.model) as handle:
        model = json.load(handle)
    scratch = tempfile.mkdtemp(prefix="dcs-event-parity-")
    digest_entries, evidence, failures = [], {}, []
    plant = duty = standby = None
    try:
        duty_files = persistence_files(scratch, duty_decl)
        standby_files = persistence_files(scratch, standby_decl)
        plant, duty, duty_url, standby, standby_url = spawn_rig(
            args, duty_decl, standby_decl, duty_files, standby_files, tamper
        )

        # Phase 1 — the same convergence the switchover leg proves:
        # the tracking peer scans first each tick until GET /role
        # reports it `tracking`.
        ticks, standby_role, duty_role = converge_to_tracking(
            duty_url, standby_url, failures
        )
        evidence["converged"] = ticks[-1]
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": ticks,
                "duty_role": duty_role,
                "standby_role": standby_role,
            }
        )

        # Phase 2 — the counted emission set. The served registry's
        # first kind-declared event names the emitter; its component's
        # writable boolean `run` input is held through the field
        # owner's receipted path — refused `not_active` at the
        # standby's role boundary, the pair's writes-stay-gated half —
        # and the declared step table's scans run out, each tick
        # having the tracking peer re-derive the same emissions from
        # the adopted checkpoint.
        schema = get(f"{duty_url}/schema", "GET /schema", failures)
        events = simulate.declared_events(schema)
        if not events:
            failures.append(
                "event-parity-failed: the served registry declares no "
                "kind-emitted event — the leg has no counted set to "
                "drive"
            )
            raise Abort
        component, spec = events[0]
        interfaces = {
            entry["name"]: entry["interface"]
            for entry in schema.get("interfaces", [])
        }
        write = next(
            (
                entry
                for entry in interfaces.get(component, {}).get("commands", [])
                if entry.get("name") == "write_value:run"
            ),
            None,
        )
        point = write.get("point") if write is not None else None
        request_fields = (
            {a["name"]: a["kind"] for a in write.get("request", [])}
            if write is not None
            else {}
        )
        writable = {
            entry["id"] for entry in model["io_points"] if entry.get("writable")
        }
        if point is None or request_fields.get("value") != "bool" or (
            point not in writable
        ):
            failures.append(
                f"event-parity-failed: {component} serves no writable "
                f"boolean `run` input to drive {spec['name']} — the leg "
                "has no counted set to drive"
            )
            raise Abort
        command = {
            "write_value": {
                "kind": "bool",
                "point": point,
                "value": {"bool": True},
            }
        }
        status, receipt = request(
            f"{duty_url}/command", {"command": command, "actor": ACTOR}
        )
        if status != 200 or simulate.receipt_outcome(receipt) != "accepted":
            failures.append(
                f"the `run` write driving {component}'s {spec['name']} "
                f"answered {status} {receipt}, expected an accepted "
                "receipt"
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
                f"the standby's role boundary answered {status} "
                f"{refusal}, expected a rejected not_active receipt — "
                "the tracking peer's writes must stay gated"
            )
            raise Abort

        scans = simulate.emission_scans(model, component)
        if scans <= 0:
            failures.append(
                f"event-parity-failed: {component}'s declared step "
                "table drives no emission — the leg has no counted set"
            )
            raise Abort
        # The apply tick scans the owner first: a field-carried write
        # is one the quiesced peer cannot issue, so the standby must
        # not pull while the receipt still reads `Accepted` — its next
        # pull carries the checkpoint captured after the application,
        # the settled receipt and the field-side `run` value included.
        owner = scan(duty_url, failures)
        ticks = [owner["tick"]]
        # The remaining emission scans pace standby-first again: each
        # pull lands the owner's just-published checkpoint before the
        # tracking peer re-derives the tick's emissions from it.
        for _ in range(scans - 1):
            tracked = scan(standby_url, failures)
            owner = scan(duty_url, failures)
            if select_snapshot(tracked) != select_snapshot(owner):
                failures.append(
                    "the tracking peer's image diverged from the field "
                    f"owner's at tick {owner['tick']} — the adopted "
                    "state no longer tracks"
                )
                raise Abort
            ticks.append(owner["tick"])
        digest_entries.append(
            {
                "phase": "emissions",
                "component": component,
                "event": spec["name"],
                "receipt": receipt,
                "refusal": refusal,
                "ticks": ticks,
            }
        )

        # Phase 3 — the parity itself. The roles must not have moved —
        # the tracking peer still standby, the owner still active —
        # then both peers' served resource views collect their routed
        # `event_emitted` records and the standby's must equal the
        # active's: same component attribution, declared identities,
        # ordered fields, tick, and retention.
        standby_role = get(f"{standby_url}/role", "GET /role", failures)
        duty_role = get(f"{duty_url}/role", "GET /role", failures)
        sync = standby_role.get("sync")
        if standby_role.get("role") != "standby" or not (
            isinstance(sync, dict) and "tracking" in sync
        ):
            failures.append(
                "the tracking peer left its standby role across the "
                f"emission drive — GET /role answers {standby_role}"
            )
            raise Abort
        if duty_role.get("role") != "active":
            failures.append(
                f"the field owner reports {duty_role.get('role')!r} "
                "across the emission drive, expected active"
            )
            raise Abort
        duty_view = get(f"{duty_url}/resources", "GET /resources", failures)
        standby_view = get(
            f"{standby_url}/resources", "GET /resources", failures
        )
        expected = event_records(duty_view)
        counted = [
            record
            for record in expected
            if record["component"] == component
            and record["event"]["event"] == spec["name"]
        ]
        declared_component = next(
            (
                entry
                for entry in model["components"]
                if f"{entry['kind']}:{entry['id']}" == component
            ),
            {},
        )
        steps = sum(
            1
            for name in declared_component.get("parameters", {})
            if re.fullmatch(r"step_\d+_ticks", name)
        )
        if not counted:
            failures.append(
                f"event-parity-failed: the active serves no emitted "
                f"{spec['name']} record from {component} — the counted "
                "set never stood"
            )
            raise Abort
        if steps and len(counted) != steps:
            failures.append(
                f"event-parity-failed: the active serves "
                f"{len(counted)} {spec['name']} records from "
                f"{component} against the declared step table's {steps}"
            )
            raise Abort
        doctor_view(standby_view, tamper)
        records = assert_event_parity(duty_view, standby_view, expected)
        digest_entries.append(
            {
                "phase": "parity",
                "component": component,
                "event": spec["name"],
                "counted": len(counted),
                "records": records,
                "duty_role": duty_role,
                "standby_role": standby_role,
            }
        )
        evidence["final_tick"] = ticks[-1]
        evidence["counted"] = len(counted)
        evidence["event"] = spec["name"]
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        stop(standby)
        stop(duty)
        stop(plant)
        shutil.rmtree(scratch, ignore_errors=True)
    return digest_entries, evidence, failures


def pair_pass(args, tamper):
    """The pair run: converge, receipt, switch, continue, persist.
    Returns `(digest_entries, evidence, failures)`."""
    declared = manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the pair leg has "
            "nothing to exercise"
        )
    manifest, duty_decl, standby_decl = declared
    fingerprint = int(manifest["model"]["fingerprint"], 16)
    scratch = tempfile.mkdtemp(prefix="dcs-pair-")
    digest_entries, evidence, failures = [], {}, []
    plant = duty = standby = None
    try:
        duty_files = persistence_files(scratch, duty_decl)
        standby_files = persistence_files(scratch, standby_decl)
        plant, duty, duty_url, standby, standby_url = spawn_rig(
            args, duty_decl, standby_decl, duty_files, standby_files, tamper
        )

        # Phase 1 — convergence. Each driven tick scans the tracking
        # peer first — its request pulling and applying the owner's
        # latest checkpoint — then the field owner, so the peers rest
        # at the same tick with identical images.
        ticks, standby_role, duty_role = converge_to_tracking(
            duty_url, standby_url, failures
        )
        evidence["converged"] = ticks[-1]
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": ticks,
                "duty_role": duty_role,
                "standby_role": standby_role,
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
        # The settling tick: the adopted receipt log — the pair's one
        # command audit — must read identically on both peers.
        tracked = scan(standby_url, failures)
        owner = scan(duty_url, failures)
        if select_snapshot(tracked) != select_snapshot(owner):
            failures.append(
                "the tracking peer's image diverged from the field "
                f"owner's at tick {owner['tick']}"
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
        status, demote = request(f"{duty_url}/demote", {})
        if status != 200 or demote.get("role") != "demoting":
            failures.append(
                f"POST /demote on the field owner answered {status} "
                f"{demote}, expected a demoting report"
            )
            raise Abort
        status, promote = request(f"{standby_url}/promote", {})
        if status != 200 or promote.get("role") != "promoting":
            failures.append(
                f"POST /promote on the converged standby answered "
                f"{status} {promote}, expected a promoting report"
            )
            raise Abort
        evidence["switched_at"] = demote["tick"]
        digest_entries.append(
            {
                "phase": "switch",
                "refused_promote": refused,
                "demote": demote,
                "promote": promote,
            }
        )

        # Phase 4 — the run continues bumplessly: the demoted peer
        # tracks the new owner off the address its own pulls
        # announced, each driven tick keeping the peers identical, the
        # promoted peer serving active at the continuing tick.
        ticks = []
        for _ in range(HANDOVER_TICKS):
            tracked = scan(duty_url, failures)
            owner = scan(standby_url, failures)
            if select_snapshot(tracked) != select_snapshot(owner):
                failures.append(
                    "the demoted peer's image diverged from the "
                    f"promoted owner's at tick {owner['tick']} — the "
                    "switch was not bumpless"
                )
                raise Abort
            ticks.append(owner["tick"])
        duty_role = get(f"{duty_url}/role", "GET /role", failures)
        standby_role = get(f"{standby_url}/role", "GET /role", failures)
        if standby_role.get("role") != "active":
            failures.append(
                f"the promoted peer reports {standby_role.get('role')!r}, "
                "expected active"
            )
            raise Abort
        sync = duty_role.get("sync")
        if duty_role.get("role") != "standby" or not (
            isinstance(sync, dict) and "tracking" in sync
        ):
            failures.append(
                "the demoted peer never reconverged — GET /role "
                f"answers {duty_role}"
            )
            raise Abort
        receipts_duty = get(f"{duty_url}/receipts", "GET /receipts", failures)
        receipts_standby = get(
            f"{standby_url}/receipts", "GET /receipts", failures
        )
        if receipts_duty != receipts_standby:
            failures.append(
                "the peers' receipt logs diverged across the switch — "
                "the adopted audit is not one log"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "handover",
                "ticks": ticks,
                "duty_role": duty_role,
                "standby_role": standby_role,
                "receipts": receipts_standby,
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
        urls = {
            duty_decl["name"]: duty_url,
            standby_decl["name"]: standby_url,
        }
        persisted = {}
        for name in (duty_decl["name"], standby_decl["name"]):
            served = get(f"{urls[name]}/journal", "GET /journal", failures)
            transitions = role_transitions(served)
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
                        if checkpoint.get("model_fingerprint") != fingerprint:
                            failures.append(
                                f"{name}'s state file carries fingerprint "
                                f"{checkpoint.get('model_fingerprint')}, the "
                                f"manifest declares {fingerprint}"
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
        stop(standby)
        stop(duty)
        stop(plant)
        shutil.rmtree(scratch, ignore_errors=True)
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
        "--parity",
        action="store_true",
        help="run the emit-identical standby event-parity leg instead "
        "of the switchover leg",
    )
    parser.add_argument(
        "--tamper",
        choices=[
            "broken-peer-flag",
            "dropped-event-record",
            "reattributed-event-record",
        ],
        help="doctor the run — the pass must fail naming the evidence",
    )
    args = parser.parse_args()

    if args.tamper in ("dropped-event-record", "reattributed-event-record") and (
        not args.parity
    ):
        parser.error(f"--tamper {args.tamper} exercises the parity leg — pass --parity")

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    leg = "event-parity" if args.parity else "pair"
    try:
        if args.parity:
            digest_entries, evidence, failures = parity_pass(args, args.tamper)
        else:
            digest_entries, evidence, failures = pair_pass(args, args.tamper)
    except Abort as abort:
        for line in abort.args:
            eprint(f"{leg}: {line}")
        return 1
    for failure in failures:
        if failure.startswith(f"{leg}-failed:"):
            eprint(failure)
        else:
            eprint(f"{leg}: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"{leg}: the {args.tamper} case passed silently — the leg "
                "never noticed the doctored run"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    if args.parity:
        print(
            f"event-parity-digest {digest} — {evidence['counted']} "
            f"counted {evidence['event']} records identical on both "
            f"peers, the standby tracking at tick "
            f"{evidence['final_tick']} (converged at "
            f"{evidence['converged']})"
        )
        return 0
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
