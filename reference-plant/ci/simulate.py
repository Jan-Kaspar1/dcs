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


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plant-server", required=True)
    parser.add_argument("--controller", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--dynamics", required=True)
    parser.add_argument("--scenario", required=True)
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
