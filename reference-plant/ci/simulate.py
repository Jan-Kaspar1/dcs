#!/usr/bin/env python3
"""The scripted deterministic simulation for the reference plant.

Drives the checked-in model and dynamics documents through the released
tooling — `dcs-plant-server` serving the simulated plant plus its
process dynamics, and `dcs-controller --driven --remote` running the
model against it, scans advancing only on `POST /scan` requests — and
asserts the declared scenario's observable outcomes leg by leg.

Usage:

    simulate.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario scenario.json

With `--surface` the script drives the same deterministic `--driven`
run but asserts the served operator surface against the emitted model's
declaration instead of the scenario legs — the check's `surface` stage:

- `GET /signals` must serve exactly the signal index the emitted model
  declares — every declared point carrying its signal's name, unit,
  description, and group and the point's direction, value type, and
  declared writability, so the station's writable command points appear
  writable while the never-shelvable alarm's read-only `shelve` point
  does not — plus one component record per declared instance;
- `GET /` must serve the monitoring page;
- `GET /schema` must serve the block-interface registry — one versioned
  interface per declared `<kind>:<id>` covering every declared port as
  a measurement or state resource with its bound point, every declared
  parameter as a configuration entry with its `set_parameter` command,
  the point-command verbs on every `In` port, and the block-level
  `command_settled`/`step_failed` events; with `--schema-out PATH` the
  served document is also written to PATH for the check's
  schema-conformance leg (`ci/schema_conformance.py`);
- every kind-declared command a served interface carries answers a
  structured receipt through `POST /command`'s `invoke` variant and
  settles `applied` through the journaled `command_settled` record;
- every kind-declared event a served interface carries must reach the
  consumer-visible record — the `GET /journal` `event_emitted` entries
  and the instance-attributed `events` of `GET /resources` — once the
  run drives its declaring component to emission (the exercise
  program's `run` input held across its declared step table);
- the snapshot's `descriptors` must cover every composed component as
  its declared `<kind>:<id>`;
- `GET /journal` must answer the run's recorded transitions.

Each mismatch is reported as a `surface: …` line on stderr and the run
exits 1; the check reports that as `surface-mismatch`.

The scenario document — `pump-station --scenario` emits it — declares
`dt` (simulated seconds per scan) and `legs`, each leg carrying:

- `name` — the leg's name, reported on failure;
- `commands` — optional list of `POST /command` bodies submitted before
  the leg's scans, each in the `{"command": …, "actor": …}` envelope;
- `expect_receipts` — the outcome each command's receipt must carry:
  `"accepted"`, or a rejection reason name like `"not_writable"`;
- `plant` — optional list of raw `dcs-plant-server` protocol requests
  (`{"op": "inject_fault", …}` / `{"op": "clear_fault", …}`) issued on a
  dedicated connection before the leg's scans — the unfenced
  field-side fault surface;
- `scans` — how many scans `POST /scan` advances;
- `expect` — `{ "<point id>": <expectation> }` asserted on the returned
  snapshot: `{"bool": …}`, `{"int": …}`, `{"float": …}` for an exact
  value, or `{"min": …, "max": …}` for an inclusive float range.

The run is request-timed and deterministic end to end: identical runs
produce identical outcomes. On success the script prints one
`scenario-digest <sha256>` line over the leg outcomes — the check
compares two runs' digests — and exits 0. A failed leg names itself and
the offending point or receipt on stderr and exits 1.
"""

import argparse
import contextlib
import hashlib
import json
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.request


def eprint(*args):
    print(*args, file=sys.stderr)


class PlantClient:
    """One line-delimited JSON connection to `dcs-plant-server`."""

    def __init__(self, address):
        host, port = address.rsplit(":", 1)
        self.socket = socket.create_connection((host, int(port)), timeout=10)
        self.stream = self.socket.makefile("rw")

    def request(self, request):
        self.stream.write(json.dumps(request) + "\n")
        self.stream.flush()
        return json.loads(self.stream.readline())

    def close(self):
        self.socket.close()


def http(url, body=None):
    """POST a JSON body (or GET when body is None); returns the decoded
    JSON response."""
    if body is None:
        with urllib.request.urlopen(url) as response:
            return json.load(response)
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def http_text(url):
    """GET a non-JSON resource; returns the decoded body."""
    with urllib.request.urlopen(url) as response:
        return response.read().decode()


def listen_address(process, what):
    """Reads the child's stderr until its `listening on <addr>` line."""
    for line in process.stderr:
        line = line.strip()
        if "listening on" in line:
            return line.rsplit(None, 1)[-1]
    raise RuntimeError(f"{what} exited without reporting a listen address")


def snapshot_point(snapshot, point):
    """The point's latest sample value from a TelemetrySnapshot."""
    for entry in snapshot["points"]:
        if entry["point"] == point:
            sample = entry.get("sample")
            if sample is None:
                return None
            return sample["value"]
    raise KeyError(point)


def check_expectation(name, point, expectation, value):
    """Returns a mismatch description, or None when the expectation
    holds."""
    if value is None:
        return f"{name}: point {point} reports no sample"
    if "bool" in expectation:
        if value != {"bool": expectation["bool"]}:
            return f"{name}: point {point} reads {value}, expected {expectation}"
    elif "int" in expectation:
        if value != {"int": expectation["int"]}:
            return f"{name}: point {point} reads {value}, expected {expectation}"
    elif "float" in expectation:
        if value != {"float": expectation["float"]}:
            return f"{name}: point {point} reads {value}, expected {expectation}"
    else:
        if "float" not in value:
            return f"{name}: point {point} reads {value}, expected a float"
        low = expectation.get("min", float("-inf"))
        high = expectation.get("max", float("inf"))
        if not low <= value["float"] <= high:
            return f"{name}: point {point} reads {value}, expected {expectation}"
    return None


def receipt_outcome(receipt):
    """The outcome name a CommandReceipt carries: `accepted`/`applied`
    or the rejection reason's variant name."""
    outcome = receipt["outcome"]
    if "rejected" in outcome:
        reason = outcome["rejected"]["reason"]
        return next(iter(reason))
    return next(iter(outcome))


def declared_signal_index(model):
    """The signal index the emitted model document declares — the same
    derived view `GET /signals` serves, resolved here from the document
    itself so the check proves the served surface against the artifact,
    not against another consumer of it. Mirrors the contract's
    resolution rules: lowest-signal-id wins for a shared point, a
    signal-less point defaults to `point-<id>`, and `direction`,
    `value_type`, and `writable` always come from the point."""
    signals_by_point = {}
    for signal in model["signals"]:
        current = signals_by_point.get(signal["source"])
        if current is None or signal["id"] < current["id"]:
            signals_by_point[signal["source"]] = signal
    points = []
    for point in sorted(model["io_points"], key=lambda entry: entry["id"]):
        signal = signals_by_point.get(point["id"])
        points.append(
            {
                "point": point["id"],
                "signal": signal["id"] if signal else None,
                "name": signal["name"] if signal else f"point-{point['id']}",
                "direction": point["direction"],
                "value_type": point["value_type"],
                "unit": signal.get("unit") if signal else None,
                "description": signal.get("description") if signal else None,
                "group": signal.get("group") if signal else None,
                "writable": bool(point.get("writable", False)),
            }
        )
    components = []
    for instance in model["components"]:
        record = {
            "name": f"{instance['kind']}:{instance['id']}",
            "kind": instance["kind"],
        }
        if "rationalization" in instance:
            record["rationalization"] = instance["rationalization"]
        components.append(record)
    return {"points": points, "components": components}


def index_mismatches(declared, served):
    """Named differences between the emitted model's declared signal
    index and the index `GET /signals` serves — one message per
    offending point or component, in declared order."""
    failures = []
    served_points = {entry["point"]: entry for entry in served["points"]}
    for want in declared["points"]:
        got = served_points.get(want["point"])
        if got is None:
            failures.append(
                f"point {want['point']} ({want['name']}) is declared but not served"
            )
            continue
        for field in (
            "signal",
            "name",
            "direction",
            "value_type",
            "unit",
            "description",
            "group",
            "writable",
        ):
            if got.get(field) != want[field]:
                failures.append(
                    f"point {want['point']} ({want['name']}): served "
                    f"{field}={got.get(field)!r}, declared {want[field]!r}"
                )
    declared_ids = [entry["point"] for entry in declared["points"]]
    for point in sorted(set(served_points) - set(declared_ids)):
        failures.append(f"point {point} is served but not declared")
    if [entry["point"] for entry in served["points"]] != declared_ids:
        if set(served_points) == set(declared_ids):
            failures.append("the served index is not ordered by point id")
    served_components = {entry["name"]: entry for entry in served["components"]}
    for want in declared["components"]:
        got = served_components.get(want["name"])
        if got is None:
            failures.append(
                f"component {want['name']} is declared but not served"
            )
            continue
        for field in ("kind", "rationalization"):
            if got.get(field) != want.get(field):
                failures.append(
                    f"component {want['name']}: served "
                    f"{field}={got.get(field)!r}, declared {want.get(field)!r}"
                )
    for name in sorted(
        set(served_components) - {entry["name"] for entry in declared["components"]}
    ):
        failures.append(f"component {name} is served but not declared")
    return failures


def descriptor_mismatches(model, snapshot):
    """Named differences between the composed components and the
    descriptors the snapshot serves — every declared instance must
    appear as a `<kind>:<id>` descriptor carrying its kind."""
    failures = []
    descriptors = {entry["name"]: entry for entry in snapshot["descriptors"]}
    declared = {
        f"{instance['kind']}:{instance['id']}": instance["kind"]
        for instance in model["components"]
    }
    for name, kind in declared.items():
        descriptor = descriptors.get(name)
        if descriptor is None:
            failures.append(
                f"component {name} is declared but serves no descriptor"
            )
        elif descriptor["kind"] != kind:
            failures.append(
                f"component {name}: served kind={descriptor['kind']!r}, "
                f"declared {kind!r}"
            )
    for name in sorted(set(descriptors) - set(declared)):
        failures.append(f"component {name} serves a descriptor but is not declared")
    return failures


def bound_points(model):
    """The `(component id, port name) -> point id` map the model's
    wiring resolves — the binding the served registry's `point`
    annotation carries. A point-to-port connection binds the declared
    point; a port-to-port wire binds the synthesized internal carrier
    pair the assembly allocates above every declared point id — the
    `from` port the `Out` carrier, the `to` port the linked `In` — in
    connection order, the same resolution `dcs-assembly` performs."""
    bound = {}
    next_internal = (
        max((point["id"] for point in model["io_points"]), default=0) + 1
    )
    for connection in model["connections"]:
        ports = [
            end["port"] for end in (connection["from"], connection["to"]) if "port" in end
        ]
        point = next(
            (
                end["point"]
                for end in (connection["from"], connection["to"])
                if "point" in end
            ),
            None,
        )
        if len(ports) == 1 and point is not None:
            bound[(ports[0]["component"], ports[0]["name"])] = point
        elif len(ports) == 2:
            bound[(ports[0]["component"], ports[0]["name"])] = next_internal
            bound[(ports[1]["component"], ports[1]["name"])] = next_internal + 1
            next_internal += 2
    return bound


def registry_expectations(model):
    """The content each declared component's served interface must
    carry — derived from the emitted model document, so
    `schema_mismatches` proves the served registry against the
    artifact and the test seam can build a tamperable served-shaped
    document from the same expectation.

    Returns `{name: {"kind", "ports", "configuration", "commands",
    "events"}}` where `ports` maps a port name to the resource fields
    asserted (`direction`, `kind`, `point`), `configuration` maps a
    parameter name to its declared value kind, `commands` maps an
    adapted command's name to its asserted fields (`adapted`,
    `availability`, `point`), and `events` is the name set the
    interface must carry."""
    bound = bound_points(model)
    expected = {}
    for component in model["components"]:
        name = f"{component['kind']}:{component['id']}"
        ports = {}
        commands = {}
        events = {"command_settled", "step_failed"}
        for port_name, port in component["ports"].items():
            point = bound.get((component["id"], port_name))
            ports[port_name] = {
                "direction": port["direction"],
                "kind": port["value_type"],
                "point": point,
            }
            events.add(f"quality_changed:{port_name}")
            if port["value_type"] in ("bool", "int"):
                events.add(f"point_changed:{port_name}")
            if port["direction"] == "in":
                for verb in ("write_value", "force_point", "unforce_point"):
                    commands[f"{verb}:{port_name}"] = {
                        "adapted": verb,
                        "availability": "bound_point_writable",
                        "point": point,
                    }
        configuration = {}
        for parameter_name, parameter in component.get("parameters", {}).items():
            configuration[parameter_name] = next(iter(parameter))
            commands[f"set_parameter:{parameter_name}"] = {
                "adapted": "set_parameter",
                "availability": "always",
            }
        expected[name] = {
            "kind": component["kind"],
            "ports": ports,
            "configuration": configuration,
            "commands": commands,
            "events": events,
        }
    return expected


def schema_mismatches(model, schema):
    """Named differences between the emitted model's declared
    components and the block-interface registry `GET /schema` serves —
    every declared `<kind>:<id>` must serve a versioned interface
    carrying its declared ports as measurement/state resources, its
    declared parameters as configuration, the adapted command verbs on
    every `In` port, and the adapted event vocabulary."""
    failures = []
    served = {}
    for entry in schema.get("interfaces", []):
        served[entry.get("name")] = entry.get("interface", {})
    for name, want in registry_expectations(model).items():
        interface = served.get(name)
        if interface is None:
            failures.append(
                f"component {name} is declared but the registry serves no interface"
            )
            continue
        if interface.get("kind") != want["kind"]:
            failures.append(
                f"component {name}: served kind={interface.get('kind')!r}, "
                f"declared {want['kind']!r}"
            )
        if interface.get("version") != 1:
            failures.append(
                f"component {name}: served interface version "
                f"{interface.get('version')!r}, expected 1"
            )
        resources = {}
        for collection in ("measurements", "state"):
            for entry in interface.get(collection, []):
                resources[entry.get("name")] = entry
        configuration = {
            entry.get("name"): entry
            for entry in interface.get("configuration", [])
        }
        commands = {
            entry.get("name"): entry for entry in interface.get("commands", [])
        }
        events = {entry.get("name") for entry in interface.get("events", [])}
        for port_name, port in want["ports"].items():
            resource = resources.get(port_name)
            if resource is None:
                failures.append(
                    f"component {name}: port {port_name} is declared but "
                    f"serves no measurement or state resource"
                )
            else:
                for field in ("direction", "kind", "point"):
                    if resource.get(field) != port[field]:
                        failures.append(
                            f"component {name}: port {port_name} serves "
                            f"{field}={resource.get(field)!r}, "
                            f"declared {port[field]!r}"
                        )
        for command_name, command in want["commands"].items():
            spec = commands.get(command_name)
            if spec is None:
                failures.append(
                    f"component {name}: serves no {command_name} command"
                )
                continue
            for field, expected in command.items():
                if spec.get(field) != expected:
                    failures.append(
                        f"component {name}: {command_name} serves "
                        f"{field}={spec.get(field)!r}, declared {expected!r}"
                    )
        for parameter_name, kind in want["configuration"].items():
            property_ = configuration.get(parameter_name)
            if property_ is None:
                failures.append(
                    f"component {name}: parameter {parameter_name} is "
                    f"declared but serves no configuration entry"
                )
            elif property_.get("kind") != kind:
                failures.append(
                    f"component {name}: parameter {parameter_name} serves "
                    f"kind={property_.get('kind')!r}, declared {kind!r}"
                )
        for event_name in sorted(want["events"]):
            if event_name not in events:
                failures.append(
                    f"component {name}: serves no {event_name} event"
                )
    for name in sorted(set(served) - set(registry_expectations(model))):
        failures.append(f"component {name} serves an interface but is not declared")
    return failures


def declared_commands(schema):
    """The `(component, spec)` pairs a served registry declares
    natively — `adapted == "declared"` `commands` entries, the
    `invoke`-addressed surface."""
    return [
        (entry["name"], spec)
        for entry in schema.get("interfaces", [])
        for spec in entry["interface"].get("commands", [])
        if spec.get("adapted") == "declared"
    ]


def declared_events(schema):
    """The `(component, spec)` pairs a served registry declares
    natively — `adapted == "declared"` `events` entries, the
    kind-emitted surface."""
    return [
        (entry["name"], spec)
        for entry in schema.get("interfaces", [])
        for spec in entry["interface"].get("events", [])
        if spec.get("adapted") == "declared"
    ]


def command_arguments(spec):
    """A minimal submission honoring a declared command's request
    schema — one typed argument per declared `request` entry."""
    minimal = {"bool": {"bool": False}, "int": {"int": 1}, "float": {"float": 1.0}}
    return {
        argument["name"]: minimal[argument["kind"]]
        for argument in spec.get("request", [])
    }


def receipt_mismatches(component, command, receipt):
    """Named differences between the structured receipt a declared
    command's `invoke` submission must answer and what `POST /command`
    returned — the receipt must echo the submission and carry the
    `accepted` outcome the admissible-at-rest invocation earns."""
    name = f"{command['name']} on {component}"
    if not isinstance(receipt, dict):
        return [f"the declared command {name} produced no receipt"]
    failures = []
    invoke = receipt.get("command", {}).get("invoke", {})
    if invoke.get("component") != component or invoke.get("command") != command["name"]:
        failures.append(
            f"the declared command {name}'s receipt echoes "
            f"{receipt.get('command')!r}"
        )
    outcome = receipt.get("outcome")
    if not isinstance(outcome, dict) or not (
        {"accepted", "applied", "rejected"} & set(outcome)
    ):
        failures.append(f"the declared command {name} produced no structured receipt")
    else:
        outcome_name = receipt_outcome(receipt)
        if outcome_name != "accepted":
            failures.append(
                f"the declared command {name} answered {outcome_name}, "
                f"expected accepted"
            )
    return failures


def settlement_misses(invoked, journal):
    """Each `(component, command)` submitted through `invoke` must
    reach a journaled `command_settled` receipt with the `applied`
    outcome — the structured answer completing at the scan boundary."""
    settled = [
        entry["event"]["command_settled"]["receipt"]
        for entry in journal
        if "command_settled" in entry.get("event", {})
    ]
    failures = []
    for component, command in invoked:
        matches = [
            receipt
            for receipt in settled
            if receipt.get("command", {}).get("invoke", {}).get("component")
            == component
            and receipt["command"]["invoke"].get("command") == command
        ]
        if not matches:
            failures.append(
                f"the declared command {command} on {component} produced "
                f"no settled receipt"
            )
        elif all(receipt_outcome(receipt) != "applied" for receipt in matches):
            failures.append(
                f"the declared command {command} on {component} settled "
                f"{receipt_outcome(matches[-1])}, not applied"
            )
    return failures


def emitted_event_misses(wanted, journal):
    """Each `(component, spec)` in `wanted` must have a journaled
    `event_emitted` record carrying its declared payload fields — the
    kind-emitted event reaching the consumer-visible record."""
    emitted = [
        entry["event"]["event_emitted"]["event"]
        for entry in journal
        if "event_emitted" in entry.get("event", {})
    ]
    failures = []
    for component, spec in wanted:
        matches = [
            event
            for event in emitted
            if event.get("component") == component
            and event.get("event") == spec["name"]
        ]
        if not matches:
            failures.append(
                f"no emitted {spec['name']} event from {component} "
                f"reached the journal"
            )
            continue
        fields = matches[-1].get("fields", {})
        for field in spec.get("payload", []):
            if field["name"] not in fields:
                failures.append(
                    f"the emitted {spec['name']} event from {component} "
                    f"lacks declared field {field['name']}"
                )
    return failures


def resource_event_misses(wanted, resources):
    """Each `(component, spec)` in `wanted` must appear in the
    component's `GET /resources` `events` — the per-instance view of
    the same consumer-visible record."""
    components = {
        entry.get("name"): entry for entry in resources.get("components", [])
    }
    failures = []
    for component, spec in wanted:
        entry = components.get(component)
        if entry is None:
            failures.append(f"{component} serves no resource view")
            continue
        if not any(
            "event_emitted" in event.get("event", {})
            and event["event"]["event_emitted"]["event"].get("event")
            == spec["name"]
            for event in entry.get("events", [])
        ):
            failures.append(
                f"no emitted {spec['name']} event is attributed to "
                f"{component} in the resource view"
            )
    return failures


def emission_scans(model, component):
    """The running scans a declared-event component needs to emit — the
    exercise program's `sequencer` emits `step_completed` once `run`
    has held across a step's declared ticks, so the declared step
    table's total length covers emission."""
    declared = next(
        (
            entry
            for entry in model["components"]
            if f"{entry['kind']}:{entry['id']}" == component
        ),
        None,
    )
    if declared is None:
        return 0
    ticks = 0
    for name, value in declared.get("parameters", {}).items():
        if re.fullmatch(r"step_\d+_ticks", name) and "int" in value:
            ticks += value["int"]
    return ticks


def run_surface(monitor, model, schema_out=None):
    """The `--surface` mode's check: asserts the monitor's served
    operator surface against the emitted model's declaration, over the
    same deterministic `--driven` run the scenario mode performs.
    `schema_out` records the served `GET /schema` document to a file —
    the check's schema-conformance leg then checks it against the
    release record's artifact."""
    failures = []
    declared = declared_signal_index(model)
    try:
        served = http(f"{monitor}/signals")
    except urllib.error.URLError as error:
        failures.append(f"GET /signals answered {error}")
        served = None
    if served is not None:
        failures += index_mismatches(declared, served)
    try:
        page = http_text(f"{monitor}/")
    except urllib.error.URLError as error:
        failures.append(f"GET / answered {error}")
    else:
        if "<html" not in page:
            failures.append("GET / did not serve the monitoring page")

    # The served block-interface registry: `GET /schema` must cover
    # every component the emitted model declares — the schema-driven
    # contract a generic consumer renders from.
    try:
        schema = http(f"{monitor}/schema")
    except urllib.error.URLError as error:
        failures.append(f"GET /schema answered {error}")
        schema = None
    if schema is not None:
        if schema_out is not None:
            with open(schema_out, "w") as handle:
                json.dump(schema, handle, indent=2, sort_keys=True)
        failures += schema_mismatches(model, schema)

    # The declared command surface: every kind-declared command a
    # served interface carries submits through `POST /command`'s
    # `invoke` variant and must answer a structured receipt. The
    # submissions queue now and apply at the first scan boundary the
    # `POST /scan` below crosses.
    invoked = []
    if schema is not None:
        commands = declared_commands(schema)
        if not commands:
            failures.append(
                "the registry carries no kind-declared command — the "
                "declared command surface is unproven"
            )
        for component, command in commands:
            body = {
                "command": {
                    "invoke": {
                        "component": component,
                        "command": command["name"],
                        "arguments": command_arguments(command),
                    }
                },
                "actor": "ci-surface",
            }
            try:
                receipt = http(f"{monitor}/command", body)
            except urllib.error.URLError as error:
                failures.append(
                    f"the declared command {command['name']} on {component} "
                    f"produced no receipt: {error}"
                )
                continue
            failures += receipt_mismatches(component, command, receipt)
            invoked.append((component, command["name"]))

    # The kind-emitted event surface: each component declaring a
    # kind-emitted event is driven far enough to emit it. Driving is
    # composition knowledge — the exercise program's `sequencer` emits
    # `step_completed` once its writable `run` input has held across
    # the declared step table's ticks.
    wanted = []
    scans_needed = 2
    if schema is not None:
        writable = {entry["point"] for entry in declared["points"] if entry["writable"]}
        interfaces = {
            entry["name"]: entry["interface"] for entry in schema["interfaces"]
        }
        events = declared_events(schema)
        if not events:
            failures.append(
                "the registry carries no kind-declared event — the "
                "emitted event surface is unproven"
            )
        for component, event in events:
            wanted.append((component, event))
            interface = interfaces.get(component, {})
            write = next(
                (
                    entry
                    for entry in interface.get("commands", [])
                    if entry.get("name") == "write_value:run"
                ),
                None,
            )
            point = write.get("point") if write is not None else None
            request = {a["name"]: a["kind"] for a in write.get("request", [])} if write else {}
            if point is None or request.get("value") != "bool" or point not in writable:
                failures.append(
                    f"{component}: no writable boolean `run` input drives it to "
                    f"emit {event['name']}"
                )
                continue
            try:
                receipt = http(
                    f"{monitor}/command",
                    {
                        "command": {
                            "write_value": {
                                "kind": "bool",
                                "point": point,
                                "value": {"bool": True},
                            }
                        },
                        "actor": "ci-surface",
                    },
                )
                outcome = receipt_outcome(receipt)
            except (urllib.error.URLError, KeyError, TypeError) as error:
                failures.append(
                    f"{component}: the `run` write produced no receipt: {error}"
                )
                continue
            if outcome != "accepted":
                failures.append(
                    f"{component}: the `run` write answered {outcome}, "
                    f"expected accepted"
                )
                continue
            scans_needed = max(scans_needed, emission_scans(model, component) + 1)

    try:
        snapshot = http(f"{monitor}/scan", {"scans": scans_needed})
    except urllib.error.URLError as error:
        failures.append(f"POST /scan answered {error}")
        snapshot = None
    if snapshot is not None:
        failures += descriptor_mismatches(model, snapshot)

    # The consumer-visible record: `GET /journal` answers the run's
    # transitions — which must include each submitted declared
    # command's settled receipt and each declared event's emitted
    # record — and `GET /resources` attributes the emitted events to
    # their producing instances.
    journal = None
    try:
        journal = http(f"{monitor}/journal")
    except urllib.error.URLError as error:
        failures.append(f"GET /journal answered {error}")
    if journal is not None and not isinstance(journal, list):
        failures.append("GET /journal did not answer a list of entries")
        journal = None
    if isinstance(journal, list):
        if not journal:
            failures.append("GET /journal answered no entries across the run's scans")
        else:
            failures += emitted_event_misses(wanted, journal)
            failures += settlement_misses(invoked, journal)
    try:
        resources = http(f"{monitor}/resources")
    except urllib.error.URLError as error:
        failures.append(f"GET /resources answered {error}")
        resources = None
    if resources is not None:
        failures += resource_event_misses(wanted, resources)

    if failures:
        for failure in failures:
            eprint(f"surface: {failure}")
        return 1
    emitted = sum(
        1 for entry in journal if "event_emitted" in entry.get("event", {})
    )
    digest = hashlib.sha256(
        json.dumps(
            {
                "signals": served,
                "descriptors": snapshot["descriptors"],
                "schema": schema,
                "journal": journal,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    writable = sum(1 for entry in declared["points"] if entry["writable"])
    print(
        f"surface-digest {digest} — {len(declared['points'])} points "
        f"({writable} writable), {len(declared['components'])} components, "
        f"{len(schema['interfaces'])} interfaces, {len(invoked)} declared "
        f"commands receipted, {emitted} emitted events, "
        f"{len(journal)} journal entries"
    )
    return 0


@contextlib.contextmanager
def driven_rig(plant_server, controller, model, dynamics, dt):
    """Spawns the simulated plant plus the driven controller against it
    — `dcs-plant-server <model> --dynamics <doc>` and `dcs-controller
    <model> --remote <addr> --driven --dt <t>` — and yields the pair's
    addresses `(plant_addr, monitor_url)`, the monitor's `POST /scan`
    pacing the run. Both processes are terminated on exit."""
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
    try:
        plant_addr = listen_address(plant, "dcs-plant-server")
        controller_process = subprocess.Popen(
            [
                controller,
                model,
                "--remote",
                plant_addr,
                "--driven",
                "--listen",
                "127.0.0.1:0",
                "--dt",
                str(dt),
            ],
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            monitor_addr = listen_address(controller_process, "dcs-controller")
            yield plant_addr, f"http://{monitor_addr}"
        finally:
            controller_process.terminate()
            controller_process.wait(timeout=10)
    finally:
        plant.terminate()
        plant.wait(timeout=10)


def run_legs(monitor, plant_client, legs, between_legs=None):
    """Runs the scenario's legs against the driven monitor — plant
    fault requests on the dedicated plant connection, receipted
    commands through `POST /command`, scans through `POST /scan`, and
    each leg's declared point expectations on the returned snapshot.
    `between_legs` runs after each leg's assertions with that leg's
    index — the seam a consumer schedule uses to place a mid-run event.

    Returns `(digest_entries, failures)`: the per-leg outcome records
    (name, tick, receipt outcomes, observed point values) a run's
    digest covers, and the named mismatches found."""
    digest_entries = []
    failures = []
    for index, leg in enumerate(legs):
        name = leg["name"]
        for request in leg.get("plant", []):
            response = plant_client.request(request)
            if response.get("result") == "error" or "error" in response:
                failures.append(
                    f"{name}: plant request {request} answered {response}"
                )
        commands = leg.get("commands", [])
        receipts = leg.get("expect_receipts", [])
        if len(commands) != len(receipts):
            raise RuntimeError(f"{name}: commands/expect_receipts lengths differ")
        outcomes = []
        for body, expected in zip(commands, receipts):
            receipt = http(f"{monitor}/command", body)
            outcome = receipt_outcome(receipt)
            outcomes.append(outcome)
            if outcome != expected:
                failures.append(
                    f"{name}: command receipt is {outcome}, "
                    f"expected {expected}"
                )
        if leg["scans"]:
            snapshot = http(f"{monitor}/scan", {"scans": leg["scans"]})
        else:
            snapshot = http(f"{monitor}/snapshot")
        for point_text, expectation in sorted(
            leg.get("expect", {}).items(), key=lambda item: int(item[0])
        ):
            point = int(point_text)
            try:
                value = snapshot_point(snapshot, point)
            except KeyError:
                failures.append(f"{name}: point {point} is not served")
                continue
            mismatch = check_expectation(name, point, expectation, value)
            if mismatch:
                failures.append(mismatch)
        digest_entries.append(
            {
                "leg": name,
                "tick": snapshot["tick"],
                "receipts": outcomes,
                "observed": {
                    point: snapshot_point(snapshot, int(point))
                    for point in sorted(leg.get("expect", {}), key=int)
                },
            }
        )
        if between_legs:
            between_legs(index)
    return digest_entries, failures


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plant-server", required=True)
    parser.add_argument("--controller", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--dynamics", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument(
        "--surface",
        action="store_true",
        help="assert the served operator surface against the emitted "
        "model instead of running the scenario legs",
    )
    parser.add_argument(
        "--schema-out",
        help="with --surface, write the served GET /schema document to "
        "this path for the check's schema-conformance leg",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        scenario = json.load(handle)

    with driven_rig(
        args.plant_server,
        args.controller,
        args.model,
        args.dynamics,
        scenario["dt"],
    ) as (plant_addr, monitor):
        if args.surface:
            with open(args.model) as handle:
                model = json.load(handle)
            return run_surface(monitor, model, args.schema_out)
        plant_client = PlantClient(plant_addr)
        digest_entries, failures = run_legs(
            monitor, plant_client, scenario["legs"]
        )
        if failures:
            for failure in failures:
                eprint(f"scenario: {failure}")
            return 1
        digest = hashlib.sha256(
            json.dumps(digest_entries, sort_keys=True).encode()
        ).hexdigest()
        print(f"scenario-digest {digest}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
