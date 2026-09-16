#!/usr/bin/env python3
"""The `dcs-ctl` leg of the reference plant's clean-CI check (WW-ENG-003,
WW-FND-003): the release set's shipped operator CLI exercised end to end
against the simulate stage's deterministic `--driven` rig —
`dcs-plant-server` serving the checked-in model and dynamics,
`dcs-controller --driven` advancing scans on `POST /scan` requests —
where `dcs-ctl` itself is the only driver: `scan` issues the run's
pacing, the read subcommands answer the served block-interface surface,
and the receipted subcommands carry the mutations.

The proving kind is the emitted model's `sequencer` — the exercise
program whose kind-declared `advance`/`reset` commands and kind-emitted
`step_completed` event the leg invokes and observes. The leg asserts,
naming each failure on stderr:

- the read subcommands answer the served contract: `signals` serves
  exactly the signal index the emitted model declares, `schema` the
  sequencer's interface carrying `advance`/`reset` as declared commands
  and `step_completed` as a kind-emitted event, `snapshot` the
  descriptors covering every composed component, and `events`/
  `resources` the per-instance live view;
- a kind-declared `invoke` prints the `accepted` receipt and settles
  `applied` at the scan boundary — the settled receipt visible through
  `receipts` and journaled as `command_settled` carrying the declared
  `--actor`;
- `resources` reports the per-command availability — a command bound
  to a point the model never marked writable reads `available: false`
  beside the named refusal, and the kind-declared `advance`'s own
  refusal settles `command_refused` into the component's attributed
  events once the completed table's predicate holds;
- the refusal modes exit nonzero naming the failure rather than
  passing silently: an undeclared command answers `unknown_command`,
  a malformed `invoke` argument fails its declared-kind parse, and an
  unreachable monitor names its address.

On success the script prints one `ctl-digest <sha256>` line over the
leg's deterministic record — the served read payloads, the answered
receipts, the settled and emitted journal entries, the per-command
availability, and the named refusal observations — which `ci/check.sh`
compares across two passes (`ctl-nondeterministic`). A failed
assertion exits 1 with its evidence on stderr (`ctl-failed`).

Usage:

    ctl.py --ctl PATH --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario scenario.json
"""

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys

import simulate

# The actor the leg's receipted submissions declare — the attributed
# identity the receipts and journaled `command_settled` entries carry.
ACTOR = "ci-ctl"


def eprint(*args):
    print(*args, file=sys.stderr)


def invoke_cli(binary, addr, arguments):
    """One `dcs-ctl` invocation; returns the CompletedProcess.

    `DCS_ACTOR` is scrubbed so an ambient configured actor can never
    attribute an invocation the leg declares itself — the same
    isolation the tool's own suite keeps.
    """
    env = dict(os.environ)
    env.pop("DCS_ACTOR", None)
    return subprocess.run(
        [binary, addr, *arguments],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def outcome(receipt):
    """The outcome variant a printed receipt carries — `accepted`,
    `applied`, or `rejected` — or None on a malformed answer."""
    if not isinstance(receipt, dict):
        return None
    value = receipt.get("outcome")
    if not isinstance(value, dict) or not value:
        return None
    return next(iter(value))


def rejection_name(receipt):
    """The named rejection a `rejected` receipt carries — the
    `CommandError`'s wire variant — or None."""
    if outcome(receipt) != "rejected":
        return None
    reason = receipt["outcome"]["rejected"].get("reason")
    if isinstance(reason, dict) and reason:
        return next(iter(reason))
    return reason if isinstance(reason, str) else None


def emitted_step(entry):
    """The `step` field of an `event_emitted` journal entry's
    `step_completed` payload — `{"value": {"int": n}}` on the wire —
    or None for any other entry."""
    emitted = entry.get("event", {}).get("event_emitted", {}).get("event", {})
    if emitted.get("event") != "step_completed":
        return None
    return emitted.get("fields", {}).get("step")


def closed_port():
    """An address nothing listens on — a just-released ephemeral port —
    for the unreachable-monitor refusal."""
    stream = socket.socket()
    stream.bind(("127.0.0.1", 0))
    port = stream.getsockname()[1]
    stream.close()
    return f"127.0.0.1:{port}"


def settled_receipts(journal, command=None):
    """The journaled `command_settled` receipts, optionally only those
    whose invoke names `command`."""
    receipts = [
        entry["event"]["command_settled"]["receipt"]
        for entry in journal
        if "command_settled" in entry.get("event", {})
    ]
    if command is None:
        return receipts
    return [
        receipt
        for receipt in receipts
        if receipt.get("command", {}).get("invoke", {}).get("command") == command
    ]


def invoke_receipts(receipts, command):
    """The receipt log's entries for `invoke` submissions of `command`."""
    return [
        receipt
        for receipt in receipts
        if receipt.get("command", {}).get("invoke", {}).get("command") == command
    ]


def verdicts_for(snapshot, component):
    """The component's `command_verdicts` entry in a served snapshot —
    the post-scan availability probe's standing verdicts."""
    return next(
        (
            entry.get("verdicts", [])
            for entry in snapshot.get("command_verdicts", [])
            if entry.get("name") == component
        ),
        [],
    )


def run(args):
    """One leg pass over a fresh driven rig; returns the process exit
    code after printing the digest or the failures."""
    with open(args.model) as handle:
        model = json.load(handle)
    with open(args.scenario) as handle:
        scenario = json.load(handle)

    declared = next(
        (entry for entry in model["components"] if entry["kind"] == "sequencer"),
        None,
    )
    if declared is None:
        eprint("ctl: the emitted model declares no sequencer — the leg's proving kind")
        return 1
    component = f"sequencer:{declared['id']}"
    run_point = simulate.bound_points(model).get((declared["id"], "run"))
    writable = {point["id"] for point in model["io_points"] if point.get("writable")}
    if run_point is None or run_point not in writable:
        eprint(f"ctl: {component}'s run input binds no model-declared writable point")
        return 1

    failures = []
    # The digest's record: the leg's deterministic evidence, kept free
    # of the per-run listen addresses a refused invocation's stderr
    # names.
    record = {"component": component, "submissions": [], "refusals": []}

    with simulate.driven_rig(
        args.plant_server,
        args.controller,
        args.model,
        args.dynamics,
        scenario["dt"],
    ) as (_plant_addr, monitor):
        addr = monitor.removeprefix("http://")

        def answered(*cli):
            """A subcommand expected to succeed: returns the decoded
            answer, or None with the failure recorded."""
            result = invoke_cli(args.ctl, addr, [str(arg) for arg in cli])
            if result.returncode != 0:
                failures.append(
                    f"dcs-ctl {' '.join(map(str, cli))} exited "
                    f"{result.returncode}: {result.stderr.strip()}"
                )
                return None
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                failures.append(
                    f"dcs-ctl {' '.join(map(str, cli))} printed no contract payload"
                )
                return None

        def scanned(scans, tick):
            """`dcs-ctl scan <n>` — the run's drive channel — asserting
            the returned snapshot's tick."""
            snapshot = answered("scan", scans)
            if snapshot is not None and snapshot.get("tick") != tick:
                failures.append(
                    f"scan {scans}: the run reports tick "
                    f"{snapshot.get('tick')}, expected {tick}"
                )

        def submitted(label, receipt):
            """Records a receipted submission's printed answer and
            checks it answered `accepted` under the declared actor."""
            if receipt is None:
                return
            record["submissions"].append({"label": label, "receipt": receipt})
            name = outcome(receipt)
            if name != "accepted":
                failures.append(f"{label}: the submission answered {name}, expected accepted")
            if receipt.get("actor") != ACTOR:
                failures.append(
                    f"{label}: the receipt carries actor {receipt.get('actor')!r}, "
                    f"expected {ACTOR!r}"
                )

        # --- The read surface over the same run ---------------------

        signals = answered("signals")
        if signals is not None:
            record["signals"] = signals
            for failure in simulate.index_mismatches(
                simulate.declared_signal_index(model), signals
            ):
                failures.append(f"signals: {failure}")

        schema = answered("schema")
        if schema is not None:
            interface = next(
                (
                    entry.get("interface", {})
                    for entry in schema.get("interfaces", [])
                    if entry.get("name") == component
                ),
                None,
            )
            if interface is None:
                failures.append(f"schema: {component} serves no interface")
            else:
                record["interface"] = interface
                if interface.get("kind") != "sequencer":
                    failures.append(
                        f"schema: {component} serves kind {interface.get('kind')!r}"
                    )
                commands = {
                    spec.get("name"): spec for spec in interface.get("commands", [])
                }
                advance = commands.get("advance")
                if (
                    not isinstance(advance, dict)
                    or advance.get("adapted") != "declared"
                    or advance.get("availability") != "kind_declared"
                    or advance.get("request") != [{"name": "count", "kind": "int"}]
                ):
                    failures.append(
                        f"schema: {component}'s declared `advance` serves {advance!r}"
                    )
                reset = commands.get("reset")
                if (
                    not isinstance(reset, dict)
                    or reset.get("adapted") != "declared"
                    or reset.get("availability") != "always"
                    or reset.get("request") != []
                ):
                    failures.append(
                        f"schema: {component}'s declared `reset` serves {reset!r}"
                    )
                events = {
                    spec.get("name"): spec for spec in interface.get("events", [])
                }
                emitted = events.get("step_completed")
                if not isinstance(emitted, dict) or emitted.get("adapted") != "declared":
                    failures.append(
                        f"schema: {component}'s `step_completed` event serves {emitted!r}"
                    )

        snapshot = answered("snapshot")
        if snapshot is not None:
            record["descriptors"] = snapshot.get("descriptors")
            for failure in simulate.descriptor_mismatches(model, snapshot):
                failures.append(f"snapshot: {failure}")

        # --- The receipted command path -----------------------------

        # `write` holds the sequencer's `run` input — the receipted
        # path's ordinary point write, attributed. Two held scans run
        # the first step's declared ticks out: `step_completed` emits.
        submitted(
            "write run",
            answered("write", run_point, "true", "--actor", ACTOR),
        )
        scanned(2, 2)

        events = answered("events", component)
        if events is not None:
            if not any(
                emitted_step(entry) == {"value": {"int": 1}} for entry in events
            ):
                failures.append(
                    f"events: no emitted step_completed from {component} "
                    "reached the attributed record"
                )
        keyed = answered("events")
        if keyed is not None and component not in keyed:
            failures.append(f"events: the all-components view carries no {component} key")

        # The kind-declared `advance` through `invoke`: accepted at
        # submission, applied at the boundary — from the second step the
        # single-step advance completes the table.
        submitted(
            "invoke advance",
            answered("invoke", component, "advance", "count=1", "--actor", ACTOR),
        )
        scanned(1, 3)

        receipts = answered("receipts")
        if receipts is not None:
            advance = invoke_receipts(receipts, "advance")
            if not advance:
                failures.append("receipts: the invoke produced no settled receipt")
            elif outcome(advance[-1]) != "applied":
                failures.append(
                    f"receipts: the invoke settled {outcome(advance[-1])}, "
                    "expected applied"
                )
            elif advance[-1].get("actor") != ACTOR:
                failures.append(
                    "receipts: the settled invoke carries no actor attribution"
                )

        journal = answered("journal")
        if journal is not None:
            journaled = settled_receipts(journal, "advance")
            if not journaled:
                failures.append("journal: no command_settled for the invoke")
            elif outcome(journaled[-1]) != "applied":
                failures.append(
                    f"journal: the invoke settled {outcome(journaled[-1])}, "
                    "expected applied"
                )
            elif journaled[-1].get("actor") != ACTOR:
                failures.append("journal: the invoke's command_settled carries no actor")

        # The standing availability the snapshot publishes for the
        # kind-declared commands: on the completed table `advance`
        # reports unavailable with the kind's named refusal.
        snapshot = answered("snapshot")
        if snapshot is not None:
            advance = next(
                (
                    verdict
                    for verdict in verdicts_for(snapshot, component)
                    if verdict.get("name") == "advance"
                ),
                None,
            )
            if advance is None or advance.get("available") is not False:
                failures.append(
                    f"snapshot: the completed table's `advance` verdict serves "
                    f"{advance!r}"
                )
            elif "run to its end" not in (advance.get("refusal") or ""):
                failures.append(
                    f"snapshot: the `advance` verdict names no refusal: {advance!r}"
                )

        # The same refused invocation through the receipted path:
        # admission accepts, the boundary settles `command_refused` —
        # the named refusal the resource view's attributed events then
        # carry beside the per-command availability.
        submitted(
            "invoke advance refused",
            answered("invoke", component, "advance", "--actor", ACTOR),
        )
        scanned(1, 4)

        resources = answered("resources", component)
        if resources is not None:
            command_states = {
                entry.get("name"): entry for entry in resources.get("commands", [])
            }
            record["command_states"] = command_states
            for name in ("advance", "reset"):
                state = command_states.get(name)
                if not isinstance(state, dict) or state.get("available") is not True:
                    failures.append(
                        f"resources: {name} serves {state!r} — the declared "
                        "commands stay admissible"
                    )
            held_reset = command_states.get("write_value:reset")
            if (
                not isinstance(held_reset, dict)
                or held_reset.get("available") is not False
                or "not declared writable" not in (held_reset.get("refusal") or "")
            ):
                failures.append(
                    f"resources: write_value:reset serves {held_reset!r} — "
                    "the unwritable bound point's named refusal"
                )
            refused = [
                receipt
                for receipt in settled_receipts(resources.get("events", []), "advance")
                if rejection_name(receipt) == "command_refused"
            ]
            if not refused:
                failures.append(
                    f"resources: no command_refused receipt is attributed to {component}"
                )
            elif "run to its end" not in json.dumps(
                refused[-1]["outcome"]["rejected"]["reason"]
            ):
                failures.append(
                    "resources: the command_refused receipt names no declared reason"
                )

        # `reset` — the Always-available declared command — applies and
        # reopens `advance`.
        submitted(
            "invoke reset",
            answered("invoke", component, "reset", "--actor", ACTOR),
        )
        scanned(1, 5)
        snapshot = answered("snapshot")
        if snapshot is not None:
            advance = next(
                (
                    verdict
                    for verdict in verdicts_for(snapshot, component)
                    if verdict.get("name") == "advance"
                ),
                None,
            )
            if not isinstance(advance, dict) or advance.get("available") is not True:
                failures.append(
                    f"snapshot: `advance` stays refused after reset: {advance!r}"
                )

        # --- The refusal modes: nonzero, the failure named ----------

        # An undeclared command on a served component answers the
        # `unknown_command` rejection — the rejected receipt still
        # prints, and the exit is nonzero naming the CommandError.
        result = invoke_cli(args.ctl, addr, ["invoke", component, "bogus"])
        if result.returncode == 0:
            failures.append("dcs-ctl invoke of an undeclared command exited zero")
        elif "unknown_command" not in result.stderr:
            failures.append(
                f"the undeclared command's failure names {result.stderr.strip()!r}, "
                "not unknown_command"
            )
        else:
            try:
                rejected = json.loads(result.stdout)
            except json.JSONDecodeError:
                rejected = None
            if rejection_name(rejected) == "unknown_command":
                record["refusals"].append("unknown_command")
            else:
                failures.append("the undeclared command printed no rejected receipt")

        # A malformed invoke argument fails its declared-kind parse —
        # `count` is `advance`'s declared Int — before any submission.
        result = invoke_cli(args.ctl, addr, ["invoke", component, "advance", "count=two"])
        if result.returncode == 0:
            failures.append("dcs-ctl invoke with a malformed argument exited zero")
        elif "invalid value" not in result.stderr:
            failures.append(
                f"the malformed argument's failure names {result.stderr.strip()!r}"
            )
        else:
            record["refusals"].append("invalid-value")

        # An unreachable monitor names its address on stderr.
        dead = closed_port()
        result = invoke_cli(args.ctl, dead, ["snapshot"])
        if result.returncode == 0:
            failures.append("dcs-ctl against an unreachable monitor exited zero")
        elif dead not in result.stderr:
            failures.append(
                f"the unreachable monitor's failure names {result.stderr.strip()!r}, "
                f"not {dead}"
            )
        else:
            record["refusals"].append("unreachable-monitor")

        # The final receipted-path record: every settled submission —
        # the write, both `advance` invocations (applied, then
        # command_refused), `reset`, and the admission-rejected undeclared
        # command — in the log and the journal.
        receipts = answered("receipts")
        if receipts is not None:
            record["receipts"] = receipts
        journal = answered("journal")
        if journal is not None:
            record["journaled"] = [
                entry
                for entry in journal
                if "command_settled" in entry.get("event", {})
                or "event_emitted" in entry.get("event", {})
            ]
        events = answered("events", component)
        if events is not None:
            record["events"] = events

    if failures:
        for failure in failures:
            eprint(f"ctl: {failure}")
        return 1
    digest = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
    print(
        f"ctl-digest {digest} — {len(record['submissions'])} receipted "
        f"submissions, {len(record.get('journaled', []))} settled/emitted "
        f"journal entries, {len(record['refusals'])} named refusals"
    )
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ctl", required=True, help="the released dcs-ctl binary")
    parser.add_argument("--plant-server", required=True)
    parser.add_argument("--controller", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--dynamics", required=True)
    parser.add_argument("--scenario", required=True)
    args = parser.parse_args()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
