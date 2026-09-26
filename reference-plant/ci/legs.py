#!/usr/bin/env python3
"""The pair stage's leg driver — the check's `== pair ==` stage is a
directory of self-registering legs rather than a list inside
`check.sh`: every `ci/legs/<name>.py` file declares a module-level
`LEG` literal this driver reads (parsed, never imported) and runs.

Adding a leg is exactly one new file under `ci/legs/` — no edit to
`check.sh`, the boundary lint, or any other shared file. The file is
a runnable script taking the common pair-stage arguments (plus its
own `--tamper` choices) whose docstring carries the leg's explanatory
prose and whose `LEG` literal carries its stage registration:

    LEG = {
        "order": 20,                     # the leg's recorded position
                                         # — unique across the
                                         # directory; gaps leave room
                                         # between legs
        "title": "the checkpoint-negotiation leg",
        "passes": "negotiation-leg",     # named in the leg's
                                         # nondeterministic diagnostic
        "failed": "divergence-missed",   # optional — the leg's failed
                                         # diagnostic when it is not
                                         # <stem>-failed
        "tools": {"ctl": "dcs-ctl"},     # optional — each entry passes
                                         # the leg `--<flag> <binary>`
                                         # resolved under --tools
        "upgrade_tools": {               # optional — like "tools" but
            "upgrade-controller":        # resolved under
                "dcs-controller",        # --upgrade-tools, the recorded
        },                               # upgrade-from revision's
                                         # tooling directory
        "tampers": [                     # optional — the leg's doctored
                                         # cases, each a `--tamper`
                                         # choice the leg accepts
            {
                "name": "expect-tracking",
                "passed": "…",           # the <stem>-unchecked
                                         # diagnostic's body when the
                                         # tampered run exits zero
                "missed": "…",           # …when the named evidence is
                                         # absent from its output
                "evidence": ["…"],       # required output substrings
            },
        ],
    }

The leg's diagnostic stem is its file name with underscores turned to
dashes — `ci/legs/stale_checkpoint.py` reports `stale-checkpoint-*`.
Discovery is deterministic: the directory's regular `*.py` entries,
ordered by each leg's declared `order`. A `*.py` file without a `LEG`
literal, a malformed record, or two legs declaring the same order
fails the stage by name — a leg is never silently dropped; entries
outside the convention (non-`.py` files, subdirectories) are ignored
by name.

Each discovered leg runs twice and the two passes must produce
identical digests; each declared tamper then runs once and must fail
carrying its named evidence. The legs share the launch/settle/restore
harness — `ci/legs/pair.py`'s `launch_pair`/`PairRig`, consolidated
under #647 — and each leg restores the pair's launch roles for the
next.

Usage:

    legs.py --plant-server PATH --controller PATH --model PATH \
        --dynamics PATH --scenario PATH --manifest PATH --tools DIR \
        [--upgrade-tools DIR]
"""

import argparse
import ast
import os
import subprocess
import sys


def eprint(*args):
    print(*args, file=sys.stderr)


class Invalid(Exception):
    """A file under ci/legs/ violates the leg convention."""


def leg_stem(filename):
    """The leg's diagnostic stem — its file name with underscores
    turned to dashes (`stale_checkpoint.py` -> `stale-checkpoint`)."""
    return filename[:-3].replace("_", "-")


def read_leg(path):
    """Parse the file's module-level `LEG` literal without importing
    the module — discovery must never execute leg code."""
    with open(path, encoding="utf-8") as source:
        tree = ast.parse(source.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "LEG"
            for target in node.targets
        ):
            try:
                return ast.literal_eval(node.value)
            except (ValueError, SyntaxError) as error:
                raise Invalid(
                    f"{path}: the LEG literal does not evaluate: {error}"
                )
    raise Invalid(f"{path}: a leg file must declare a LEG literal")


def validate_leg(path, record):
    """The LEG record's required shape — refuse a malformed one rather
    than let a leg register partially."""
    name = os.path.basename(path)
    if not isinstance(record, dict):
        raise Invalid(f"{path}: LEG must be a dict literal")
    for field in ("order", "title", "passes"):
        if field not in record:
            raise Invalid(f"{path}: LEG must declare {field!r}")
    if not isinstance(record["order"], int):
        raise Invalid(f"{path}: LEG['order'] must be an integer")
    for field in ("title", "passes"):
        if not isinstance(record[field], str):
            raise Invalid(f"{path}: LEG[{field!r}] must be a string")
    if "failed" in record and not isinstance(record["failed"], str):
        raise Invalid(f"{path}: LEG['failed'] must be a string")
    for field in ("tools", "upgrade_tools"):
        if field in record:
            tools = record[field]
            if not isinstance(tools, dict) or not all(
                isinstance(flag, str) and isinstance(binary, str)
                for flag, binary in tools.items()
            ):
                raise Invalid(
                    f"{path}: LEG[{field!r}] must map flags to binaries"
                )
    for tamper in record.get("tampers", []):
        if not isinstance(tamper, dict) or not all(
            isinstance(tamper.get(field), str)
            for field in ("name", "passed", "missed")
        ) or not (
            isinstance(tamper.get("evidence"), list)
            and all(isinstance(item, str) for item in tamper["evidence"])
            and tamper["evidence"]
        ):
            raise Invalid(
                f"{path}: every LEG tamper must declare name, passed, "
                "missed, and a nonempty evidence list"
            )
    record["file"] = path
    record["stem"] = leg_stem(name)
    return record


def discover(legs_dir):
    """The stage's legs in declared order — deterministic discovery
    over the directory's regular `*.py` files; entries outside the
    convention are ignored by name, a `*.py` file carrying no LEG
    literal is refused, and two legs may not share an order."""
    legs = []
    for entry in sorted(os.listdir(legs_dir)):
        path = os.path.join(legs_dir, entry)
        if not os.path.isfile(path) or not entry.endswith(".py"):
            continue
        legs.append(validate_leg(path, read_leg(path)))
    if not legs:
        raise Invalid(f"{legs_dir}: the stage found no leg files")
    seen = {}
    for leg in legs:
        earlier = seen.get(leg["order"])
        if earlier is not None:
            raise Invalid(
                f"{leg['file']} and {earlier} both declare order "
                f"{leg['order']}"
            )
        seen[leg["order"]] = leg["file"]
    legs.sort(key=lambda leg: leg["order"])
    return legs


def run_leg(args, leg, tamper=None):
    """One leg invocation — the common pair-stage arguments, the leg's
    declared tool flags resolved under --tools, and the doctored case
    when one is named."""
    argv = [
        sys.executable,
        leg["file"],
        "--plant-server",
        args.plant_server,
        "--controller",
        args.controller,
        "--model",
        args.model,
        "--dynamics",
        args.dynamics,
        "--scenario",
        args.scenario,
        "--manifest",
        args.manifest,
    ]
    for flag, binary in leg.get("tools", {}).items():
        argv += ["--" + flag, os.path.join(args.tools, binary)]
    for flag, binary in leg.get("upgrade_tools", {}).items():
        argv += ["--" + flag, os.path.join(args.upgrade_tools, binary)]
    if tamper is not None:
        argv += ["--tamper", tamper]
    if tamper is None:
        # Evidence lines stream to stderr live; the digest line is
        # captured for the two-pass comparison.
        return subprocess.run(argv, stdout=subprocess.PIPE, text=True)
    return subprocess.run(
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plant-server", required=True)
    parser.add_argument("--controller", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--dynamics", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument(
        "--tools",
        required=True,
        help="the directory holding the released tooling — the legs' "
        "declared tool flags resolve to executables under it",
    )
    parser.add_argument(
        "--upgrade-tools",
        help="the directory holding the recorded upgrade-from "
        "revision's tooling — the legs' declared upgrade_tools flags "
        "resolve to executables under it",
    )
    args = parser.parse_args(argv)

    legs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "legs")
    try:
        legs = discover(legs_dir)
    except Invalid as invalid:
        eprint(f"pair-legs-invalid: {invalid}")
        return 1

    for leg in legs:
        stem = leg["stem"]
        failed = leg.get("failed", f"{stem}-failed")
        for binary in leg.get("tools", {}).values():
            binary_path = os.path.join(args.tools, binary)
            if not os.access(binary_path, os.X_OK):
                eprint(
                    f"{failed}: the release tooling ships no {binary} "
                    "binary"
                )
                return 1
        for binary in leg.get("upgrade_tools", {}).values():
            if args.upgrade_tools is None:
                eprint(
                    f"{failed}: {leg['title']} declares upgrade tooling "
                    "but --upgrade-tools was not given"
                )
                return 1
            binary_path = os.path.join(args.upgrade_tools, binary)
            if not os.access(binary_path, os.X_OK):
                eprint(
                    f"{failed}: the upgrade-from tooling ships no "
                    f"{binary} binary"
                )
                return 1
        first = run_leg(args, leg)
        if first.returncode != 0:
            eprint(
                f"{failed}: {leg['title']} did not hold — its evidence "
                "lines are above"
            )
            return 1
        second = run_leg(args, leg)
        if second.returncode != 0:
            eprint(
                f"{failed}: {leg['title']} did not hold — its evidence "
                "lines are above"
            )
            return 1
        if first.stdout != second.stdout:
            eprint(
                f"{stem}-nondeterministic: two {leg['passes']} passes "
                "produced different digests"
            )
            return 1
        print("  " + first.stdout.rstrip("\n"))
        for tamper in leg.get("tampers", []):
            proc = run_leg(args, leg, tamper["name"])
            out = proc.stdout or ""
            if proc.returncode == 0:
                eprint(f"{stem}-unchecked: {tamper['passed']}")
                return 1
            if not all(item in out for item in tamper["evidence"]):
                eprint(f"{stem}-unchecked: {tamper['missed']}: {out}")
                return 1
            print(f"  {tamper['name']}: reported, {failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
