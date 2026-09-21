#!/usr/bin/env python3
"""The alarm-validation leg for the reference plant — the consumer-side
proof that the released `dcs-controller --check` enforces decision 70's
rationalization record on a customer-owned document (WW-ALM-001,
WW-ENG-003).

The tooling stage's acceptance half only ever feeds the released
tooling the well-formed emitted document; this leg proves the
rejection half at the customer boundary. The run:

- the declaration half — every managed alarm instance in the emitted
  `model/plant.json` must carry the record's two halves: the
  `rationalization` prose block (consequence, required action,
  reference — each non-empty) and the `priority`/`class`/
  `response_ticks` parameters as declared non-negative `Int`s;
- the doctored half — copies of the emitted document, each breaking
  one managed alarm instance's record, must be refused by the released
  `dcs-controller --check` with the rejection naming the offending
  element — never a silent load or an advisory-only finding:
  - `missing-required-action` drops the `required_action` key from one
    instance's `rationalization` block — the load-path refusal names
    the missing field;
  - `empty-required-action` leaves the same field empty — the record's
    own "missing or empty" case, refused at alarm-kind construction:
    the assembly rejection naming the field and the component;
  - `missing-priority` drops the `priority` parameter on the other
    managed kind — the construction rejection naming the parameter
    and the component;
- the served half — a `dcs-controller --driven` instance on the
  emitted document serves the same record: `GET /signals`' components
  section carries each managed alarm's `rationalization` block and
  `GET /snapshot`'s parameters section reports its live
  `priority`/`class`/`response_ticks` — the declared-once consistency
  the surface stage's served sections also check.

Usage:

    alarm_validation.py --controller PATH --model model/plant.json

On success one `alarm-validation-digest <sha256>` line prints — the
check runs two passes and compares them
(`alarm-validation-nondeterministic`). A contract violation reports
`alarm-validation: …` lines on stderr and exits 1 — the check's
`alarm-validation-failed`. `--tamper skip-doctoring` writes the
pristine document where each doctored copy belongs, so `--check`
accepts every one — proving the leg's refusal assertion fires rather
than passing an unenforced rejection silently.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile

import simulate


def eprint(*args):
    print(*args, file=sys.stderr)


# The managed alarm kinds — every declared instance of either carries
# the decision-70 record.
MANAGED_ALARM_KINDS = ("managed-latching-alarm", "managed-bool-latching-alarm")

# The prose fields a complete rationalization block carries.
PROSE_FIELDS = ("consequence", "required_action", "reference")

# The record's numeric half — the descriptor-declared `Int` parameters
# every managed alarm instance carries.
CODE_PARAMETERS = ("priority", "class", "response_ticks")

# The doctored cases, each mapped to the evidence its refusal must
# carry: the offending element named in the tooling's rejection, and —
# for the cases engineered to reach alarm-kind construction — the
# assembly rejection's "failed to build" wording. A removed
# `required_action` key is refused earlier, at load, where the document
# shape names the missing field; the emptied field is the same record
# gap the construction seam itself rejects.
CASES = (
    "missing-required-action",
    "empty-required-action",
    "missing-priority",
)
CASE_EVIDENCE = {
    "missing-required-action": ("required_action",),
    "empty-required-action": ("required_action", "failed to build"),
    "missing-priority": ("priority", "failed to build"),
}


def managed_alarms(model):
    """The declared managed alarm instances, in document order."""
    return [
        instance
        for instance in model["components"]
        if instance["kind"] in MANAGED_ALARM_KINDS
    ]


def instance_name(instance):
    """The instance's diagnostic name — `<kind>:<id>`."""
    return f"{instance['kind']}:{instance['id']}"


def declaration_misses(model):
    """Named gaps in the emitted document's alarm record — every
    managed alarm instance must carry a complete `rationalization`
    block and the three non-negative `Int` codes."""
    failures = []
    alarms = managed_alarms(model)
    if not alarms:
        failures.append(
            "the model declares no managed alarm instance — the "
            "rationalization contract has nothing to enforce"
        )
    for instance in alarms:
        name = instance_name(instance)
        block = instance.get("rationalization")
        if not isinstance(block, dict):
            failures.append(f"{name} carries no rationalization block")
        else:
            for field in PROSE_FIELDS:
                if not str(block.get(field) or "").strip():
                    failures.append(
                        f"{name}: rationalization field {field} is "
                        "missing or empty"
                    )
        parameters = instance.get("parameters") or {}
        for key in CODE_PARAMETERS:
            value = parameters.get(key)
            if not (
                isinstance(value, dict)
                and isinstance(value.get("int"), int)
                and value["int"] >= 0
            ):
                failures.append(
                    f"{name}: parameter {key} is {value!r}, expected "
                    "a declared non-negative int"
                )
    return failures


def doctor(model, case):
    """A copy of the emitted document with one managed alarm
    instance's record broken; returns `(document, target)` where
    `target` is the doctored instance's diagnostic name. The prose
    cases break the first managed instance, `missing-priority` the
    first of the other managed kind, so both kinds' construction
    enforcement is proven."""
    document = json.loads(json.dumps(model))
    if case == "missing-priority":
        target = next(
            instance
            for instance in managed_alarms(document)
            if instance["kind"] == "managed-bool-latching-alarm"
        )
        del target["parameters"]["priority"]
        return document, instance_name(target)
    target = managed_alarms(document)[0]
    if case == "missing-required-action":
        del target["rationalization"]["required_action"]
    elif case == "empty-required-action":
        target["rationalization"]["required_action"] = ""
    else:
        raise ValueError(f"unknown doctoring case {case}")
    return document, instance_name(target)


def check_document(controller, path):
    """One `dcs-controller <doc> --check` run; returns `(accepted,
    output)` — `accepted` when the tooling exits zero, `output` the
    combined stdout/stderr text."""
    run = subprocess.run(
        [controller, path, "--check"],
        capture_output=True,
        text=True,
    )
    return run.returncode == 0, run.stdout + run.stderr


def served_misses(model, monitor):
    """Named divergences between the emitted document's alarm record
    and the served surface: `GET /signals`' components section must
    carry each managed alarm instance's `rationalization` block and
    `GET /snapshot`'s parameters section must report its
    `priority`/`class`/`response_ticks` live with the declared
    values."""
    failures = []
    try:
        signals = simulate.http(f"{monitor}/signals")
    except Exception as error:
        return [f"GET /signals answered {error!r}"]
    try:
        snapshot = simulate.http(f"{monitor}/snapshot")
    except Exception as error:
        return failures + [f"GET /snapshot answered {error!r}"]
    served_components = {
        entry.get("name"): entry for entry in signals.get("components", [])
    }
    reported = {
        entry.get("name"): (entry.get("values") or {})
        for entry in snapshot.get("parameters") or []
    }
    for instance in managed_alarms(model):
        name = instance_name(instance)
        record = served_components.get(name)
        if record is None:
            failures.append(f"component {name} is declared but not served")
        elif record.get("rationalization") != instance.get("rationalization"):
            failures.append(
                f"component {name}: served rationalization="
                f"{record.get('rationalization')!r}, declared "
                f"{instance.get('rationalization')!r}"
            )
        declared = instance.get("parameters") or {}
        values = reported.get(name)
        if values is None:
            failures.append(f"component {name} serves no parameter report")
            continue
        for key in CODE_PARAMETERS:
            if values.get(key) != declared.get(key):
                failures.append(
                    f"component {name}: parameter {key} serves "
                    f"{values.get(key)!r}, declared "
                    f"{declared.get(key)!r}"
                )
    return failures


def run_pass(args, tamper):
    """One leg pass: the declaration audit, the doctored `--check`
    refusals, and the served-section consistency. Returns
    `(digest_entries, evidence, failures)`."""
    digest_entries, evidence, failures = [], {}, []
    with open(args.model) as handle:
        model = json.load(handle)

    failures += declaration_misses(model)
    digest_entries.append(
        {
            "phase": "declaration",
            "instances": [
                {
                    "name": instance_name(instance),
                    "rationalization": instance.get("rationalization"),
                    "codes": {
                        key: (instance.get("parameters") or {}).get(key)
                        for key in CODE_PARAMETERS
                    },
                }
                for instance in managed_alarms(model)
            ],
        }
    )
    evidence["instances"] = len(managed_alarms(model))

    # The doctored half: each broken copy must be refused by the
    # released `--check`, the rejection naming the element the
    # doctoring removed — the contract's refusal, never a silent load.
    # A model declaring no managed alarm already failed the
    # declaration audit; there is nothing to doctor.
    refusals = []
    with tempfile.TemporaryDirectory() as scratch:
        for case in CASES if managed_alarms(model) else ():
            document, target = doctor(model, case)
            if tamper == "skip-doctoring":
                document = model
            path = f"{scratch}/{case}.json"
            with open(path, "w") as handle:
                json.dump(document, handle, indent=2)
            accepted, output = check_document(args.controller, path)
            refusal = next(
                (line for line in output.splitlines() if line.strip()), ""
            )
            refusals.append(
                {"case": case, "component": target, "refusal": refusal}
            )
            if accepted:
                failures.append(
                    f"{case}: --check accepted the doctored document "
                    f"— {target}'s broken record was never refused"
                )
                continue
            for wanted in CASE_EVIDENCE[case]:
                if wanted not in output:
                    failures.append(
                        f"{case}: --check's refusal does not name "
                        f"{wanted}: {output.strip()}"
                    )
    digest_entries.append({"phase": "doctored", "refusals": refusals})
    evidence["refusals"] = len(refusals)

    # The served half: one driven instance on the emitted document
    # serves the same record — the components section's
    # `rationalization` blocks and the parameters section's live
    # codes.
    controller = subprocess.Popen(
        [
            args.controller,
            args.model,
            "--driven",
            "--listen",
            "127.0.0.1:0",
        ],
        stderr=subprocess.PIPE,
        text=True,
    )
    served = False
    try:
        address = simulate.listen_address(controller, "dcs-controller")
        misses = served_misses(model, f"http://{address}")
        failures += misses
        served = not misses
    except Exception as error:
        failures.append(f"the served-surface run raised {error!r}")
    finally:
        controller.terminate()
        try:
            controller.wait(timeout=10)
        except subprocess.TimeoutExpired:
            controller.kill()
            controller.wait()
    evidence["served"] = served
    return digest_entries, evidence, failures


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--controller", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--tamper",
        choices=["skip-doctoring"],
        help="write the pristine document where each doctored copy "
        "belongs — `--check` accepts every one and the leg must "
        "report each acceptance",
    )
    args = parser.parse_args()

    digest_entries, evidence, failures = run_pass(args, args.tamper)
    for failure in failures:
        eprint(f"alarm-validation: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"alarm-validation: the {args.tamper} case passed "
                "silently — the leg never noticed the accepted "
                "documents"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"alarm-validation-digest {digest} — "
        f"{evidence['instances']} managed alarm instances carry the "
        f"declared record, {evidence['refusals']} doctored documents "
        "refused, served record consistent"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
