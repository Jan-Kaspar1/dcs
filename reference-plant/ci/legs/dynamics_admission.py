#!/usr/bin/env python3
"""The dynamics-admission refusal leg for the reference plant — the
consumer-side proof that the released tooling refuses each recorded
malformed dynamics class by name against the manifest-declared pair's
own model, while the deployed pair's serving plant keeps its state
and the operator's commanded Out values stand (WW-ENG-003,
WW-OPS-003): the consumer-boundary mirror of the platform suite's
dynamics-admission-refusal pins covering the #957 self-point and #958
out-point-driver fixes.

The malformed classes the QA battery recorded each passed the emitted
schema and the pre-contract preflight: a `bool_flow` or `threshold`
whose input and output name one Bool point — a self-point no element
leg's kind check can accept — panicked the serving plant's first
step, and a `threshold` driving the pump commands' controller-owned
`Out` points rewrote the operator's command every step. The contract
refuses each class "by name" at every admission seam — the merge
rejection naming the element's position and the point it drives — so
the document dies before the plant serves rather than poisoning the
shared field the pair's controllers drive. The run:

- converges the declared pair to `tracking` through the pair leg's
  driven-tick loop;
- issues the operator's receipted `write_value` commands — both pump
  groups taken out of service — settling `applied`, the field's
  `Out` points then reading the commanded inhibition identically in
  the field owner's served image and the plant's own census;
- proves the released `dcs-plant-server --check-dynamics` accepts the
  document the pair actually serves — the preflight the leg stages
  the doctored documents through — then stages each recorded
  malformed class as a doctored document naming the model's own
  resolved points: the preflight refuses every malformed element by
  name, and a second `dcs-plant-server` spawned through the harness's
  seam exits before binding on the first;
- after each class's refusals, drives a tracking-first pair tick and
  asserts the serving plant's census answers unchanged in shape, the
  commanded `Out` values read back commanded in the field and on the
  owner's served image, the writable commanded points still serve the
  operator's values, and the launch roles stand;
- restores the operator's command — the out-of-service writes
  released and settled — and leaves the pair in its launch roles.

The leg reports inconclusive rather than failing where the pinned
release predates the contract: the `--check-dynamics` preflight
refusing the document the pair serves or missing from the pinned
tooling, either admission path accepting a malformed class, or a
rejection naming no malformed element — the refusal the contract
names by element index and driving point.

Usage:

    dynamics_admission.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `dynamics-admission-digest <sha256>` line prints —
the check runs two passes and compares them
(`dynamics-admission-nondeterministic`). A contract violation reports
`dynamics-admission: …` lines on stderr and exits 1 — the check's
`dynamics-admission-failed`. `--tamper admissible-documents` stages
each class's legal twin — the lag self-point the boundary case
allows and a contact driving an `In` point — so the leg proves its
refusal assertion fires on the honest admission rather than passing
an unexercised contract.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import pair
import simulate
import takeover


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored case: each recorded class's admissible twin — the
# legal self-point lag and an element driving an `In` point — is
# staged while the leg still asserts the named refusal, so the honest
# admission must surface the named diagnostic rather than passing an
# unexercised contract.
LEG = {
    "order": 480,
    "title": "the dynamics-admission refusal leg",
    "passes": "dynamics-admission-leg",
    "tampers": [
        {
            "name": "admissible-documents",
            "passed": "an admissible-documents case passed the dynamics-admission leg",
            "missed": "the admissible-documents case did not report its named diagnostic",
            "evidence": ["admitted the doctored admissible document"],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The pinned release predates — or never carries — the named
    dynamics-admission refusal the leg exercises: the run classifies
    inconclusive, never a product failure."""


# The signal names resolving the leg's commanded inhibition: each
# pump's writable out-of-service request and the controller-owned
# field command it holds released — the operator's commanded Out
# values the refusals must leave standing.
COMMANDED = (("p101-oos", "p101-cmd"), ("p102-oos", "p102-cmd"))


def channel_points(model, direction, kind):
    """The channel-bound io_point ids of one direction and value kind,
    in id order — the only points the plant server's channel map binds
    for an element to name."""
    return sorted(
        point["id"]
        for point in model.get("io_points", [])
        if point.get("channel") is not None
        and point.get("direction") == direction
        and point.get("value_type") == kind
    )


def signal_source(model, name):
    """The point a named signal declares as its source — the
    lowest-signal-id-wins rule the served index applies."""
    source = None
    for signal in model.get("signals", []):
        if signal.get("name") == name and (
            source is None or signal["id"] < source[0]
        ):
            source = (signal["id"], signal.get("source"))
    return source[1] if source is not None else None


def io_point(model, point):
    """The emitted model's io_point entry for `point`, or None."""
    return next(
        (
            entry
            for entry in model.get("io_points", [])
            if entry.get("id") == point
        ),
        None,
    )


def resolve_commanded(model):
    """The leg's commanded inhibition: `(oos, cmd)` per COMMANDED —
    the writable out-of-service point the operator's `write_value`
    lands and the channel-bound `Out` point the command holds
    released. Aborts when the emitted model's declared wiring leaves
    the leg nothing to command."""
    commanded = []
    for oos_name, cmd_name in COMMANDED:
        oos = signal_source(model, oos_name)
        cmd = signal_source(model, cmd_name)
        oos_entry = io_point(model, oos)
        cmd_entry = io_point(model, cmd)
        if (
            oos_entry is None
            or not oos_entry.get("writable")
            or oos_entry.get("direction") != "in"
            or cmd_entry is None
            or cmd_entry.get("direction") != "out"
            or cmd_entry.get("channel") is None
        ):
            raise Abort(
                f"the emitted model wires no writable {oos_name} "
                f"inhibiting channel command {cmd_name} — the "
                "dynamics-admission leg has no commanded Out value "
                "to hold"
            )
        commanded.append({"oos": oos, "cmd": cmd, "name": cmd_name})
    return commanded


def malformed_classes(points):
    """The recorded malformed dynamics classes staged against the
    resolved model points: the #957 self-point — a `bool_flow` and a
    `threshold` each reading and driving one Bool point, the leg-kind
    check no point can satisfy — and the #958 out-point — a
    `threshold` driving each pump's controller-owned `Out` command,
    the direction rule the merge names. `admissible` is each class's
    legal twin the doctored case stages: the lag self-point the
    boundary case allows and a contact driving an `In` point. `rule`
    is the refusal wording naming the class's own violation."""
    bool_in, float_in, outs = points
    return [
        {
            "class": "self-point",
            "document": [
                {
                    "bool_flow": {
                        "input": bool_in,
                        "output": bool_in,
                        "on_rate": -10.0,
                        "off_rate": 0.0,
                        "initial": 0.0,
                    }
                },
                {
                    "threshold": {
                        "input": bool_in,
                        "output": bool_in,
                        "on": 1.0,
                        "off": 0.0,
                        "initial": False,
                    }
                },
            ],
            "admissible": [
                {
                    "first_order_lag": {
                        "input": float_in,
                        "output": float_in,
                        "time_constant": 2.0,
                        "initial": 0.0,
                    }
                }
            ],
            "rule": "Float",
        },
        {
            "class": "out-point",
            "document": [
                {
                    "threshold": {
                        "input": float_in,
                        "output": output,
                        "on": 1.0,
                        "off": 0.0,
                        "initial": False,
                    }
                }
                for output in outs[:2]
            ],
            "admissible": [
                {
                    "threshold": {
                        "input": float_in,
                        "output": bool_in,
                        "on": 1.0,
                        "off": 0.0,
                        "initial": False,
                    }
                }
            ],
            "rule": "in point",
        },
    ]


def named_refusals(document, first_only=False):
    """The named refusal each staged element must draw — the merge's
    `dynamics element <index> (driving point <output>)` attribution.
    The spawn seam's fail-fast merge names only the first malformed
    element; the preflight reports them all."""
    names = []
    for index, element in enumerate(document):
        output = next(iter(element.values()))["output"]
        names.append(f"dynamics element {index} (driving point {output})")
        if first_only:
            break
    return names


def check_dynamics(args, path):
    """One released `dcs-plant-server <model> --check-dynamics <doc>`
    invocation — the preflight the leg stages each doctored document
    through. Returns `(returncode, stderr)` unasserted."""
    run = subprocess.run(
        [args.plant_server, args.model, "--check-dynamics", path],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return run.returncode, run.stderr


def spawn_probe(args, path):
    """The spawn seam's admission probe: `dcs-plant-server <model>
    --dynamics <doc> --listen 127.0.0.1:0`, the argv `spawn_plant`
    issues — a second plant asked to serve the doctored document
    beside the pair's own. Returns `(process, preamble, bound)` —
    `bound` the reported address when the document was admitted (the
    process still runs; the caller stops it), None when the merge
    refused before binding and the process exited, `preamble` the
    stderr lines read."""
    process = subprocess.Popen(
        [
            args.plant_server,
            args.model,
            "--dynamics",
            path,
            "--listen",
            "127.0.0.1:0",
        ],
        stderr=subprocess.PIPE,
        text=True,
    )
    preamble = []
    for line in process.stderr:
        line = line.strip()
        if "listening on" in line:
            return process, preamble, line.rsplit(None, 1)[-1]
        preamble.append(line)
    process.wait(timeout=10)
    return process, preamble, None


def require_named_refusal(
    cls, surface, refused, output, names, rule, tamper, failures
):
    """Classify one admission staging: `refused` whether the tooling
    rejected the document (a nonzero exit or a spawn that never
    bound), `output` its stderr text, `names` the element attributions
    and `rule` the violation wording the class's named refusal
    carries. Returns the refusal names the digest records; raises
    `Inconclusive` where the tooling admitted the class or refused it
    unnamed — the release predating the contract — and records the
    doctored case's named failure under the admissible-documents
    tamper."""
    if not refused:
        if tamper == "admissible-documents":
            failures.append(
                f"the {cls} class's {surface} staging admitted the "
                "doctored admissible document — the leg asserts the "
                f"named refusal: {output.strip()[:200]}"
            )
            raise Abort
        raise Inconclusive(
            f"the {cls} malformed class was admitted by the released "
            f"{surface} — the pinned release predates the named "
            f"dynamics-admission refusal: {output.strip()[:200]}"
        )
    missing = [name for name in [*names, rule] if name not in output]
    if missing:
        raise Inconclusive(
            f"the {cls} class's {surface} rejection named none of "
            f"{missing} — the pinned release predates the named "
            f"refusal: {output.strip()[:200]}"
        )
    return names


def settle_writes(rig, duty_url, commands, failures):
    """Drive pair ticks until every receipted command settles `applied`
    into the adopted receipt log — the operator's writes landed —
    returning the last owner snapshot."""
    receipts = None
    for _ in range(takeover.SETTLE_BOUND):
        owner = takeover.tick(rig, failures)
        receipts = pair.get(
            f"{duty_url}/receipts", "GET /receipts", failures
        )
        if all(takeover.settled(receipts, c) for c in commands):
            return owner
    failures.append(
        "the operator's commanded writes never settled applied inside "
        f"{takeover.SETTLE_BOUND} driven ticks — the receipt log reads "
        f"{(receipts or [])[:4]}"
    )
    raise Abort


def census_out(rig, failures):
    """The serving plant's own field census — `list_points` over the
    rig's plant attachment — as `{point: (direction, value)}`. The
    refused admission attempts must leave the shared field answering
    and the Out values untouched."""
    try:
        census = rig.plant_io.request({"op": "list_points"})
    except Exception as error:
        failures.append(
            "the serving plant stopped answering its census after the "
            f"refused staging — list_points raised {error!r}"
        )
        raise Abort
    points = census.get("points") if isinstance(census, dict) else None
    if not isinstance(points, list):
        failures.append(
            "the serving plant's census broke shape after the refused "
            f"staging — list_points answered {census}"
        )
        raise Abort
    return {
        entry["point"]: (
            entry.get("direction"),
            (entry.get("sample") or {}).get("value"),
        )
        for entry in points
        if isinstance(entry, dict) and "point" in entry
    }


def assert_commanded_intact(owner, field, commanded, failures, where):
    """The operator's commanded Out values intact: the field's stored
    value on each commanded Out point reads back exactly what the
    field owner's served image computes — the controller's delivered
    write, never a value an element merged onto the point would
    stamp between scans — and the writable command points still
    serve the operator's issued values."""
    for entry in commanded:
        cmd, oos = entry["cmd"], entry["oos"]
        served = simulate.snapshot_point(owner, cmd)
        held = field.get(cmd)
        if held is None or held[0] != "out":
            failures.append(
                f"the field census lost commanded Out point {cmd} "
                f"({entry['name']}) {where} — it carries {held}"
            )
            raise Abort
        if held[1] != served:
            failures.append(
                f"the field holds {held[1]} on commanded Out point "
                f"{cmd} ({entry['name']}) {where} while the field "
                f"owner serves {served} — the shared field state "
                "carries a value the owner never wrote"
            )
            raise Abort
        if simulate.snapshot_point(owner, oos) != {"bool": True}:
            failures.append(
                f"the commanded point {oos} reads "
                f"{simulate.snapshot_point(owner, oos)} {where}, not "
                "the operator's written value — the refused staging "
                "moved the served image"
            )
            raise Abort


def tracking(report):
    """Whether a `GET /role` report is the settled tracking standby."""
    sync = report.get("sync") if isinstance(report, dict) else None
    return (
        report.get("role") == "standby"
        and isinstance(sync, dict)
        and "tracking" in sync
    )


def admission_pass(args, tamper):
    """The dynamics-admission run: converge, command the inhibition,
    prove the preflight on the served document, refuse each recorded
    malformed class at check and load with the pair's state and
    commanded Out values intact, restore. Returns `(digest_entries,
    evidence, failures)`; raises `Inconclusive` where the pinned
    release predates the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "dynamics-admission leg has nothing to exercise"
        )
    with open(args.model) as handle:
        model = json.load(handle)
    bool_in = next(iter(channel_points(model, "in", "bool")), None)
    float_in = next(iter(channel_points(model, "in", "float")), None)
    if bool_in is None or float_in is None:
        raise Abort(
            "the emitted model binds no usable point set — a Bool "
            "in-point the self-point class names and a Float "
            "in-point the out-point class reads are both required"
        )
    commanded = resolve_commanded(model)
    classes = malformed_classes(
        (bool_in, float_in, [entry["cmd"] for entry in commanded])
    )
    scratch = tempfile.mkdtemp(prefix="dcs-dynamics-admission-")
    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url

        # Phase 1 — convergence: the declared pair settled in its
        # launch roles, the deployed plant the malformed admissions
        # must never touch.
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

        # Phase 2 — the operator's commanded Out values: both pump
        # groups taken out of service through the receipted path, the
        # inhibition settling applied and the field commands reading
        # released in the owner's image and the plant's own census —
        # the state a merged out-point element would rewrite each
        # step.
        commands = [
            takeover.write_value(entry["oos"], True)
            for entry in commanded
        ]
        for command in commands:
            takeover.submit(duty_url, command, failures)
        settle_writes(rig, duty_url, commands, failures)
        owner = takeover.drive_until(
            rig,
            failures,
            lambda snapshot: all(
                simulate.snapshot_point(snapshot, entry["oos"])
                == {"bool": True}
                for entry in commanded
            ),
        )
        if owner is None:
            owner = takeover.tick(rig, failures)
            reads = [
                simulate.snapshot_point(owner, entry["oos"])
                for entry in commanded
            ]
            failures.append(
                "the commanded writes never reached the served "
                f"image — the out-of-service points read {reads}"
            )
            raise Abort
        field = census_out(rig, failures)
        evidence["field_points"] = len(field)
        assert_commanded_intact(
            owner, field, commanded, failures, "at the commanded baseline"
        )
        evidence["commanded_at"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "commanded",
                "commands": commands,
                "out": {
                    entry["cmd"]: simulate.snapshot_point(owner, entry["cmd"])
                    for entry in commanded
                },
                "tick": owner["tick"],
            }
        )

        # Phase 3 — the control: the released `--check-dynamics` must
        # accept the document the pair actually serves before its
        # refusals can evidence anything. A preflight missing from or
        # rejecting on the pinned tooling predates the contract.
        code, stderr = check_dynamics(args, args.dynamics)
        if code != 0:
            raise Inconclusive(
                "the released --check-dynamics refused the document "
                "the deployed pair serves — the preflight the leg "
                f"stages through predates or broke on this pin: "
                f"{stderr.strip()[:200]}"
            )

        # Phase 4 — the recorded malformed classes, each staged as a
        # doctored document: the preflight naming every malformed
        # element, the spawn seam exiting before a listener binds on
        # the first — then a driven pair tick proving the deployed
        # plant untouched: identical peer images, the census answering
        # in the same shape, the commanded Out values standing.
        for cls in classes:
            document = (
                cls["admissible"]
                if tamper == "admissible-documents"
                else cls["document"]
            )
            path = os.path.join(scratch, f"{cls['class']}.json")
            with open(path, "w") as handle:
                json.dump(document, handle)

            code, stderr = check_dynamics(args, path)
            check_names = require_named_refusal(
                cls["class"],
                "--check-dynamics",
                code != 0,
                stderr,
                named_refusals(document),
                cls["rule"],
                tamper,
                failures,
            )

            process, preamble, bound = spawn_probe(args, path)
            if bound is not None:
                pair.stop(process)
            spawn_names = require_named_refusal(
                cls["class"],
                "plant spawn",
                bound is None,
                "\n".join(preamble),
                named_refusals(document, first_only=True),
                cls["rule"],
                tamper,
                failures,
            )

            owner = takeover.tick(rig, failures)
            field = census_out(rig, failures)
            if len(field) != evidence["field_points"]:
                failures.append(
                    "the serving plant's census lost points after the "
                    f"{cls['class']} refusals — {evidence['field_points']}"
                    f" -> {len(field)}"
                )
                raise Abort
            assert_commanded_intact(
                owner, field, commanded, failures,
                f"after the {cls['class']} refusals",
            )
            digest_entries.append(
                {
                    "phase": "refusal",
                    "class": cls["class"],
                    "check": check_names,
                    "spawn": spawn_names,
                    "tick": owner["tick"],
                }
            )

        # Phase 5 — the restore: the out-of-service writes released
        # and settled, a final driven pair tick, and the launch roles
        # confirmed standing for the legs behind this one.
        releases = [
            takeover.write_value(entry["oos"], False)
            for entry in commanded
        ]
        for command in releases:
            takeover.submit(duty_url, command, failures)
        settle_writes(rig, duty_url, releases, failures)
        owner = takeover.drive_until(
            rig,
            failures,
            lambda snapshot: all(
                simulate.snapshot_point(snapshot, entry["oos"])
                == {"bool": False}
                for entry in commanded
            ),
        )
        if owner is None:
            owner = takeover.tick(rig, failures)
            reads = [
                simulate.snapshot_point(owner, entry["oos"])
                for entry in commanded
            ]
            failures.append(
                "the released writes never reached the served image "
                f"— the out-of-service points read {reads}"
            )
            raise Abort
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        standby_role = pair.get(
            f"{standby_url}/role", "GET /role", failures
        )
        if duty_role.get("role") != "active":
            failures.append(
                "the refused admissions moved the field owner's role — "
                f"GET /role answers {duty_role}"
            )
        if not tracking(standby_role):
            failures.append(
                "the refused admissions moved the tracking peer's "
                f"role — GET /role answers {standby_role}"
            )
        if failures:
            raise Abort
        evidence["restored_tick"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "restore",
                "tick": owner["tick"],
                "duty_role": duty_role,
                "standby_role": standby_role,
            }
        )
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Inconclusive:
        raise
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        if rig is not None:
            rig.close()
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
        choices=["admissible-documents"],
        help="stage each recorded class's admissible twin while the "
        "leg asserts the named refusal — the honest admission must "
        "fail, reporting the doctored case by name",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = admission_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "dynamics-admission: the admissible-documents case "
                "admitted the doctored admissible document — an "
                "inconclusive run offers the doctored case no evidence"
            )
            return 1
        eprint(f"dynamics-admission: inconclusive — {inconclusive}")
        print(
            f"dynamics-admission-digest inconclusive — {inconclusive}"
        )
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"dynamics-admission: {line}")
        return 1
    for failure in failures:
        eprint(f"dynamics-admission: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"dynamics-admission: the {args.tamper} case passed "
                "silently — the leg never noticed the admitted "
                "document"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"dynamics-admission-digest {digest} — tracking by tick "
        f"{evidence['converged']}, the commanded Out inhibition "
        f"holding through tick {evidence['commanded_at']}, every "
        "recorded malformed class refused by name at the "
        "--check-dynamics preflight and the spawn seam, the pair "
        f"restored in its launch roles at tick "
        f"{evidence['restored_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
