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
import hashlib
import json
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


def run_surface(monitor, model):
    """The `--surface` mode's check: asserts the monitor's served
    operator surface against the emitted model's declaration, over the
    same deterministic `--driven` run the scenario mode performs."""
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
    try:
        snapshot = http(f"{monitor}/scan", {"scans": 2})
    except urllib.error.URLError as error:
        failures.append(f"POST /scan answered {error}")
        snapshot = None
    if snapshot is not None:
        failures += descriptor_mismatches(model, snapshot)
    try:
        journal = http(f"{monitor}/journal")
    except urllib.error.URLError as error:
        failures.append(f"GET /journal answered {error}")
        journal = None
    if journal is not None and not isinstance(journal, list):
        failures.append("GET /journal did not answer a list of entries")
        journal = None
    if isinstance(journal, list) and not journal:
        failures.append("GET /journal answered no entries across the run's scans")
    if failures:
        for failure in failures:
            eprint(f"surface: {failure}")
        return 1
    digest = hashlib.sha256(
        json.dumps(
            {
                "signals": served,
                "descriptors": snapshot["descriptors"],
                "journal": journal,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    writable = sum(1 for entry in declared["points"] if entry["writable"])
    print(
        f"surface-digest {digest} — {len(declared['points'])} points "
        f"({writable} writable), {len(declared['components'])} components, "
        f"{len(journal)} journal entries"
    )
    return 0


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
    args = parser.parse_args()

    with open(args.scenario) as handle:
        scenario = json.load(handle)

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
    try:
        plant_addr = listen_address(plant, "dcs-plant-server")
        controller = subprocess.Popen(
            [
                args.controller,
                args.model,
                "--remote",
                plant_addr,
                "--driven",
                "--listen",
                "127.0.0.1:0",
                "--dt",
                str(scenario["dt"]),
            ],
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            monitor_addr = listen_address(controller, "dcs-controller")
            monitor = f"http://{monitor_addr}"
            if args.surface:
                with open(args.model) as handle:
                    model = json.load(handle)
                return run_surface(monitor, model)
            plant_client = PlantClient(plant_addr)
            digest_entries = []
            failures = []
            for leg in scenario["legs"]:
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
                    raise RuntimeError(
                        f"{name}: commands/expect_receipts lengths differ"
                    )
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
                        failures.append(
                            f"{name}: point {point} is not served"
                        )
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
                            for point in sorted(
                                leg.get("expect", {}), key=int
                            )
                        },
                    }
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
        finally:
            controller.terminate()
            controller.wait(timeout=10)
    finally:
        plant.terminate()
        plant.wait(timeout=10)


if __name__ == "__main__":
    sys.exit(main())
