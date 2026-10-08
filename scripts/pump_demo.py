#!/usr/bin/env python3
"""Run or operate the simulated public-pump example using the real DCS runtime."""

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MONITOR = "http://127.0.0.1:9080"
DEFAULT_PLANT = "127.0.0.1:9011"
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http(base, path, body=None):
    request = urllib.request.Request(base.rstrip("/") + path)
    if body is not None:
        request.data = json.dumps(body).encode("utf-8")
        request.add_header("Content-Type", "application/json")
    with HTTP.open(request, timeout=5) as response:
        return json.load(response)


def plant_request(address, body):
    host, port = address.rsplit(":", 1)
    with socket.create_connection((host, int(port)), timeout=5) as connection:
        with connection.makefile("rwb") as stream:
            stream.write(json.dumps(body).encode("utf-8") + b"\n")
            stream.flush()
            result = json.loads(stream.readline())
    if result.get("result") == "error":
        raise RuntimeError(f"simulated plant refused the request: {result}")
    return result


def point_named(index, name):
    matches = [point for point in index["points"] if point["name"] == name]
    if len(matches) != 1:
        raise RuntimeError(f"expected one declared signal named {name!r}, found {len(matches)}")
    return matches[0]


def write_bool(base, name, value):
    point = point_named(http(base, "/signals"), name)
    if not point["writable"] or point["value_type"] != "bool":
        raise RuntimeError(f"{name} is not a declared writable Boolean")
    receipt = http(base, "/command", {
        "command": {"write_value": {
            "point": point["point"], "kind": "bool", "value": {"bool": value},
        }},
        "actor": "pump-demo",
    })
    print(json.dumps(receipt, indent=2))
    if "accepted" not in receipt.get("outcome", {}):
        raise RuntimeError("controller refused command admission")
    # Accepted is admission. Show its terminal receipt after the paced scan
    # applies the command, rather than claiming success from the POST alone.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        for entry in http(base, "/journal"):
            settled = entry.get("event", {}).get("command_settled", {}).get("receipt", {})
            if receipt.get("submission") and settled.get("submission") == receipt["submission"]:
                print("Settlement:", json.dumps(settled["outcome"]))
                if "applied" not in settled["outcome"]:
                    raise RuntimeError("controller rejected command at the scan boundary")
                return
        time.sleep(0.1)
    raise RuntimeError("no terminal receipt observed within five seconds; inspect the journal")


def status(base):
    index = http(base, "/signals")
    snapshot = http(base, "/snapshot")
    samples = {point["point"]: point for point in snapshot["points"]}
    names = {point["point"]: point["name"] for point in index["points"]}
    print(f"Controller tick: {snapshot['tick']}")
    equipment = index.get("equipment", [])
    if not equipment:
        raise RuntimeError("the running model has no equipment metadata; rebuild the demo from this source")
    for item in equipment:
        print(f"\n{item['label']} ({item['id']}):")
        for identity in item["points"]:
            sample = samples.get(identity, {}).get("sample")
            print(f"  {names[identity]}: {json.dumps(sample)}")


def executable(directory, name, example=False):
    suffix = ".exe" if os.name == "nt" else ""
    paths = [directory / (name + suffix)]
    if example:
        paths.insert(0, directory / "examples" / (name + suffix))
    for path in paths:
        if path.is_file():
            return path.resolve()
    raise RuntimeError(
        f"missing {name + suffix} under {directory}; use the source-built Docker demo "
        "in docs/pump-demo.md, or compile and verify this checkout with scripts/verify.py"
    )


def connect_address(listen):
    host, port = listen.rsplit(":", 1)
    return ("127.0.0.1" if host == "0.0.0.0" else host) + ":" + port


def wait_ready(process, probe, label):
    deadline = time.monotonic() + 20
    last_error = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{label} exited with status {process.returncode}; inspect the printed log directory")
        try:
            probe()
            return
        except (OSError, ValueError) as error:
            last_error = error
            time.sleep(0.1)
    raise RuntimeError(f"{label} did not become ready within twenty seconds: {last_error}")


def run(args):
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    directory = Path(args.artifacts).resolve()
    generator = executable(directory, "pump", example=True)
    controller = executable(directory, "dcs-controller")
    server = executable(directory, "dcs-plant-server")
    run_dir = Path(args.run_dir).resolve() / time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=False)
    print(f"Demo files and logs: {run_dir}", flush=True)
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    generated = subprocess.run([str(generator)], check=True, capture_output=True, **options)
    model = json.loads(generated.stdout)
    if len(model.get("equipment", [])) != 2:
        raise RuntimeError("the pump generator must declare two equipment instances; rebuild from this source")
    model_path = run_dir / "model.json"
    model_path.write_text(json.dumps(model, indent=2) + "\n", encoding="utf-8")
    subprocess.run([str(controller), str(model_path), "--check"], check=True, **options)

    processes = []
    logs = []
    try:
        def launch(name, command):
            log = (run_dir / f"{name}.log").open("wb")
            logs.append(log)
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, **options)
            processes.append(process)
            return process

        plant_address = connect_address(args.plant_listen)
        server_process = launch("plant", [str(server), str(model_path), "--listen", args.plant_listen])
        wait_ready(server_process, lambda: plant_request(plant_address, {"op": "list_points"}), "Plant server")
        monitor = "http://" + connect_address(args.listen)
        controller_process = launch("controller", [
            str(controller), str(model_path), "--remote", plant_address,
            "--listen", args.listen, "--scan-ms", "200", "--dt", "0.2",
            "--journal-file", str(run_dir / "journal.jsonl"),
        ])
        wait_ready(controller_process, lambda: http(monitor, "/health"), "Controller")
        # Fail early if an old controller silently omitted the new contract.
        if len(http(monitor, "/signals").get("equipment", [])) != 2:
            raise RuntimeError("the controller does not serve the new equipment contract; rebuild from this source")
        print(f"Open {monitor}/", flush=True)
        print("Click Pump 1 or Pump 2 in the schematic. Choose Manual and Run request.", flush=True)
        print("Ctrl+C stops both simulated processes. See docs/pump-demo.md for fault and recovery actions.", flush=True)
        if not args.no_browser:
            webbrowser.open(monitor + "/")
        while all(process.poll() is None for process in processes):
            time.sleep(0.25)
        raise RuntimeError("a demo process exited; inspect the log directory")
    except KeyboardInterrupt:
        print("\nStopping the demonstration.", flush=True)
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        for log in logs:
            log.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--monitor", default=DEFAULT_MONITOR, help="monitor URL for operator actions")
    parser.add_argument("--plant", default=DEFAULT_PLANT, help="simulated plant address for fault actions")
    actions = parser.add_subparsers(dest="action", required=True)
    launcher = actions.add_parser("run", help="generate the public example and start the simulation")
    launcher.add_argument("--artifacts", default=str(ROOT / "target" / "debug"))
    launcher.add_argument("--run-dir", default=str(ROOT / "target" / "pump-demo"))
    launcher.add_argument("--listen", default="127.0.0.1:9080")
    launcher.add_argument("--plant-listen", default=DEFAULT_PLANT)
    launcher.add_argument("--no-browser", action="store_true")
    actions.add_parser("status", help="show the declared equipment points from the actual runtime")
    for name in ("start", "stop", "fault", "recover"):
        action = actions.add_parser(name)
        action.add_argument("pump", choices=("p101", "p102"))
    mode = actions.add_parser("mode")
    mode.add_argument("pump", choices=("p101", "p102"))
    mode.add_argument("value", choices=("auto", "manual"))
    demand = actions.add_parser("automatic")
    demand.add_argument("pump", choices=("p101", "p102"))
    demand.add_argument("value", choices=("on", "off"))
    args = parser.parse_args(argv)
    if args.action == "run":
        run(args)
    elif args.action == "status":
        status(args.monitor)
    elif args.action in ("fault", "recover"):
        point = point_named(http(args.monitor, "/signals"), f"{args.pump}-thermal")
        request = {"op": "clear_fault", "point": point["point"]}
        if args.action == "fault":
            request = {"op": "inject_fault", "point": point["point"], "fault": {"quality": {"bad": "device_fault"}}}
        print(json.dumps(plant_request(args.plant, request), indent=2))
    else:
        suffix, value = {
            "start": ("hand", True), "stop": ("hand", False),
            "mode": ("mode", getattr(args, "value", None) == "manual"),
            "automatic": ("automatic-request", getattr(args, "value", None) == "on"),
        }[args.action]
        write_bool(args.monitor, f"{args.pump}-{suffix}", value)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"pump-demo: {error}", file=sys.stderr)
        sys.exit(1)
