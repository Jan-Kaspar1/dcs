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

        def persistence(entry):
            """The controller's declared persistence file basenames
            instantiated under the leg's runner-owned scratch directory
            — the manifest's container paths become per-peer files."""
            root = os.path.join(scratch, entry["name"])
            os.makedirs(root, exist_ok=True)
            return {
                field: os.path.join(root, os.path.basename(entry[field]))
                if entry.get(field)
                else None
                for field in ("state_file", "journal_file")
            }

        duty_files = persistence(duty_decl)
        standby_files = persistence(standby_decl)

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
        # The manifest's standby wiring names the peer by its
        # deployment address; the driven run wires the tracking peer at
        # the spawned duty monitor — or, under the broken-peer-flag
        # tamper, at an address nothing serves.
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

        # Phase 1 — convergence. Each driven tick scans the tracking
        # peer first — its request pulling and applying the owner's
        # latest checkpoint — then the field owner, so the peers rest
        # at the same tick with identical images.
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
