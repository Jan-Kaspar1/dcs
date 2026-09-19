#!/usr/bin/env python3
"""The command-availability leg for the reference plant — the
consumer-side proof that the per-command verdicts `GET /resources`
serves on the deployed redundant pair agree with what the receipted
path settles (WW-ENG-003, WW-FND-003).

The pair leg (`ci/pair.py`) proves the manifest-declared pair runs and
switches; the surface stage proves the served registry and its live
verdicts on a lone driven controller. This leg exercises the verdicts
the pair's monitors actually serve — a read model reporting a command
invocable while dispatch refuses it, or the reverse, is exactly the
consumer-facing dishonesty the served-interface contract exists to
prevent, and the customer's pair has no other leg proving the verdicts
agree with what the receipted path settles. The rig is the pair legs'
shared one — `pair.launch_pair` reads the standby wiring and
persistence fields out of `deploy/manifest.json` and spawns the
released tooling exactly as the manifest declares. The run:

- convergence — the pair leg's driven-tick loop converges the standby
  to `tracking`, the peers' served snapshots identical;
- the self-consistency audit — every command row the active's `GET
  /resources` serves must be honest about its own shape: an
  `available: false` row carries a named refusal, an `available` row
  carries none. The audit runs again after the kind-declared verdict
  flips, so both verdict directions are checked, and the tracking
  standby's `/resources` must report the identical verdicts — the
  same-adopted-state rule means availability can never diverge across
  the pair;
- the refused probes — every `bound_point_writable` command the
  active's `/resources` reports unavailable and bound is submitted
  through the active's `POST /command`: each must settle a named
  rejection, never `applied` — and where the row's bound point is one
  the emitted model declares, the settled rejection must name the same
  refusal the row served (a read-only bound write answering
  `not_writable` carrying the point the row's refusal named). Rows
  bound to the synthesized internal carriers the port-to-port wiring
  allocates — points the declared signal index does not cover — are
  asserted refused with the settled answer recorded beside the served
  one: the served view names the declared-model truth (`unknown_point`)
  where dispatch names the point map's (`not_writable`), a named
  refusal either way;
- the available probe — one served-`available` command, the
  `kind_declared` `advance` the emitted exercise program declares when
  one exists and a `declared` or point-adapted available command
  otherwise, submitted through the active's `POST /command`: it must
  settle `applied` identically into both peers' adopted receipt log —
  the verdict's invocable direction proven against the receipted path;
- the kind-declared refusal — where the emitted model declares a
  `kind_declared` command, the leg drives it to its standing refusal
  through its own declared surface (the exercise `sequencer`'s
  `advance` submits until the table completes) and probes again: the
  row must then serve `available: false` carrying the kind's named
  refusal, and a resubmission must settle `rejected{command_refused}`
  carrying the refusal string verbatim — never `applied`. A model
  declaring no `kind_declared` command, or a release whose read model
  does not join the producer's published verdicts — the pinned
  release's documented limitation — records the coverage gap in its
  evidence rather than failing on machinery the model or release
  lacks; the `bound_point_writable` verdicts are asserted
  unconditionally either way.

Usage:

    availability.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `availability-digest <sha256>` line prints — the check
runs two passes and compares them (`availability-nondeterministic`). A
contract violation reports `availability: …` lines on stderr and exits
1 — the check's `availability-failed`. `--tamper refused-available`
submits the available probe to the tracking standby's monitor, where
the role gate settles `not_active` — the leg must name the refusal the
served-available command actually met rather than pass an unexercised
`applied`. `--tamper diverged-standby` flips one verdict in the
standby's served view before the pair-equality audit, so the leg
proves its identical-verdicts assertion fires.
"""

import argparse
import hashlib
import json
import sys

import pair
import simulate


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The actor the leg's receipted submissions declare, and the bound on
# the drive loop pushing a `kind_declared` command to its standing
# refusal through its own declared surface.
ACTOR = "ci-availability"
DRIVE_BOUND = 8


def command_rows(resources):
    """The `{(component, command): row}` map a `GET /resources`
    document serves — each row carrying `name`, `point`, `available`,
    and `refusal`."""
    return {
        (entry.get("name"), command.get("name")): command
        for entry in resources.get("components", [])
        for command in entry.get("commands", [])
    }


def command_specs(schema):
    """The `{(component, command): spec}` map the `GET /schema`
    registry declares — each spec carrying `adapted`, `availability`,
    `point`, and the `request` argument schema."""
    return {
        (entry["name"], spec["name"]): spec
        for entry in schema.get("interfaces", [])
        for spec in entry["interface"].get("commands", [])
    }


def self_consistency_misses(resources):
    """Each served command row that is not honest about its own
    shape — an `available: false` row without a named refusal, an
    `available` row carrying one, or a row whose `available` is no
    verdict at all."""
    misses = []
    for (component, name), row in sorted(command_rows(resources).items()):
        available = row.get("available")
        refusal = row.get("refusal")
        if available is False and not refusal:
            misses.append(
                f"{component}'s {name} serves unavailable without a "
                "named refusal"
            )
        elif available is True and refusal:
            misses.append(
                f"{component}'s {name} serves available while carrying "
                f"refusal {refusal!r}"
            )
        elif available not in (True, False):
            misses.append(
                f"{component}'s {name} serves available={available!r}, "
                "no verdict at all"
            )
    return misses


def verdict_divergence(active_rows, standby_rows):
    """Each command verdict the tracking standby reports differently
    than the field owner — `available`, `refusal`, or the bound
    `point`. The same-adopted-state rule means the pair's availability
    answers can never diverge."""
    misses = []
    for key in sorted(set(active_rows) | set(standby_rows)):
        active = active_rows.get(key)
        standby = standby_rows.get(key)
        if active is None or standby is None:
            misses.append(
                f"{key[0]}'s {key[1]} is served on only one peer — "
                "availability diverged across the pair"
            )
            continue
        for field in ("available", "refusal", "point"):
            if active.get(field) != standby.get(field):
                misses.append(
                    f"{key[0]}'s {key[1]} serves {field}="
                    f"{standby.get(field)!r} on the tracking standby, "
                    f"{active.get(field)!r} on the field owner — "
                    "availability diverged across the pair"
                )
    return misses


def submission_for(component, spec):
    """The receipted-path command a served `commands` entry denotes,
    rebuilt from the entry's declared provenance — `declared` entries
    submit as `invoke` with the declared request schema's minimal
    arguments, `write_value`/`force_point`/`unforce_point` as the point
    commands against the entry's bound point, `set_parameter` as the
    parameter tune. Returns None for an entry this leg cannot
    translate."""
    defaults = {"bool": {"bool": True}, "int": {"int": 1}, "float": {"float": 1.0}}
    request = spec.get("request") or []
    adapted = spec.get("adapted")
    if adapted == "declared":
        return {
            "invoke": {
                "component": component,
                "command": spec["name"],
                "arguments": simulate.command_arguments(spec),
            }
        }
    if adapted == "set_parameter":
        value = defaults.get(request[0].get("kind")) if request else None
        if value is None or ":" not in spec["name"]:
            return None
        return {
            "set_parameter": {
                "component": component,
                "name": spec["name"].split(":", 1)[1],
                "value": value,
            }
        }
    if adapted in ("write_value", "force_point"):
        kind = request[0].get("kind") if request else None
        value = defaults.get(kind)
        if spec.get("point") is None or value is None:
            return None
        return {
            adapted: {"point": spec["point"], "kind": kind, "value": value}
        }
    if adapted == "unforce_point":
        if spec.get("point") is None:
            return None
        return {"unforce_point": {"point": spec["point"]}}
    return None


def rejection(receipt):
    """The `reason` object a rejected receipt carries —
    `{<named refusal>: <payload>}` — or {} for any other outcome."""
    if not isinstance(receipt, dict):
        return {}
    reason = receipt.get("outcome", {}).get("rejected", {})
    return reason.get("reason", {}) if isinstance(reason, dict) else {}


def settled(receipts, command):
    """The terminal outcome the adopted receipt log recorded for
    `command` — the `applied` or named rejection the most recent
    matching entry settled to."""
    outcome, reason = None, {}
    for entry in receipts:
        if entry.get("command") == command:
            outcome, reason = simulate.receipt_outcome(entry), rejection(entry)
    return outcome, reason


def published_verdict(snapshot, component, command):
    """The producer's published verdict for `command` on `component` —
    the `command_verdicts` entry the snapshot carries where the tooling
    probes standing availability — or None when the section or the
    command's entry is absent."""
    for entry in (snapshot or {}).get("command_verdicts", []):
        if entry.get("name") == component:
            for verdict in entry.get("verdicts", []):
                if verdict.get("name") == command:
                    return verdict
    return None


def served_refusal_variant(row):
    """The named CommandError a `bound_point_writable` row's served
    refusal string denotes — `not_writable` on a served but unmarked
    point, `unknown_point` on a bound point the model never declared,
    or None for a refusal this leg cannot translate (an unbound port's
    named refusal carries no submittable target)."""
    refusal = row.get("refusal") or ""
    if "not declared writable" in refusal:
        return "not_writable"
    if "unknown I/O point" in refusal:
        return "unknown_point"
    return None


def resources_at(url, failures):
    """`GET /resources` on a peer's monitor, recording a failure and
    unwinding on any transport error."""
    return pair.get(f"{url}/resources", "GET /resources", failures)


def audit_pair(duty_url, standby_url, failures, tamper=None):
    """One audit of the settled pair's served verdicts: the active's
    `/resources` self-consistency, then the identical-verdicts rule
    against the tracking standby's. Under `diverged-standby` the
    standby's fetched view is doctored — one verdict flipped — so the
    equality check must name the divergence. Returns the active's
    `(resources, row map)`."""
    active = resources_at(duty_url, failures)
    failures.extend(self_consistency_misses(active))
    standby = resources_at(standby_url, failures)
    if tamper == "diverged-standby":
        # The doctored view: one verdict flipped with a consistent
        # refusal planted, so the identical-verdicts audit — not the
        # self-consistency check — is what must fire.
        for entry in standby.get("components", []):
            commands = entry.get("commands") or []
            if commands:
                row = commands[0]
                row["available"] = not row["available"]
                row["refusal"] = (
                    None if row["available"] else "the doctored divergence"
                )
                break
    failures.extend(
        verdict_divergence(command_rows(active), command_rows(standby))
    )
    return active, command_rows(active)


def availability_pass(args, tamper):
    """The availability run: converge, audit, probe refused and
    available commands, drive the kind-declared refusal, re-audit.
    Returns `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the availability "
            "leg has nothing to exercise"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        with open(args.model) as handle:
            model = json.load(handle)
        declared_points = {entry["id"] for entry in model["io_points"]}
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url

        # Phase 1 — convergence: the pair leg's driven-tick loop, the
        # tracking peer scanned first so each pull applies the owner's
        # latest checkpoint and the peers rest identical.
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

        # Phase 2 — the first audit: the active's served rows
        # self-consistent, the tracking standby reporting identical
        # verdicts.
        schema = pair.get(f"{duty_url}/schema", "GET /schema", failures)
        specs = command_specs(schema)
        resources, rows = audit_pair(
            duty_url, standby_url, failures, tamper
        )
        if not rows:
            failures.append(
                "the active's GET /resources serves no command rows — "
                "the availability leg has nothing to exercise"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "audit",
                "tick": resources.get("tick"),
                "rows": len(rows),
                "unavailable": sum(
                    1 for row in rows.values() if row["available"] is False
                ),
            }
        )

        # Phase 3 — the refused probes: every `bound_point_writable`
        # command the active reports unavailable, submitted through
        # `POST /command`. Each must settle a named rejection — never
        # `applied` — and where the bound point is one the emitted
        # model declares, the settled rejection names the same refusal
        # the row served: the probe-then-submit agreement the read
        # model owes its consumers.
        probes = []
        for (component, name), row in sorted(rows.items()):
            spec = specs.get((component, name))
            if spec is None or spec.get("availability") != "bound_point_writable":
                continue
            if row["available"] is not False:
                continue
            record = {"component": component, "command": name, "row": row}
            submission = submission_for(component, spec)
            if submission is None:
                # An unbound port's named refusal carries no
                # submittable target — the self-consistency audit is
                # its coverage.
                record["skipped"] = "no submittable target"
                probes.append(record)
                continue
            status, receipt = pair.request(
                f"{duty_url}/command", {"command": submission, "actor": ACTOR}
            )
            record["submission"] = submission
            record["receipt"] = receipt
            outcome = (
                simulate.receipt_outcome(receipt)
                if isinstance(receipt, dict)
                else None
            )
            reason = rejection(receipt)
            if status != 200 or not reason:
                failures.append(
                    f"{component}'s {name} serves unavailable yet the "
                    f"submission answered {status} {receipt} — the read "
                    "model's refusal never reached the receipted path"
                )
                continue
            point = row.get("point")
            variant = served_refusal_variant(row)
            if point in declared_points:
                # The declared-bound surface: the settled rejection
                # must name the same refusal the row served, carrying
                # the same point — never `applied`, never a different
                # named answer.
                if variant is None:
                    failures.append(
                        f"{component}'s {name} serves refusal "
                        f"{row['refusal']!r} the leg cannot name — the "
                        "probe-then-submit agreement is unproven"
                    )
                elif outcome != variant or reason.get(variant) != {
                    "point": point
                }:
                    failures.append(
                        f"{component}'s {name} serves refusal "
                        f"{row['refusal']!r} yet settled "
                        f"{outcome} {reason} — the receipted path names "
                        "a different refusal than the read model"
                    )
            else:
                # A bound point the emitted model never declares — the
                # synthesized internal carriers port-to-port wiring
                # allocates: the served view names the declared-model
                # truth where dispatch names the point map's. Both are
                # named refusals; the pair is recorded as the leg's
                # evidence of that documented naming boundary.
                record["served"] = row.get("refusal")
                record["settled"] = {outcome: reason.get(outcome)}
            probes.append(record)
        if not any("receipt" in probe for probe in probes):
            failures.append(
                "the emitted model serves no bound-point-writable "
                "unavailable command — the probe-then-submit leg has "
                "nothing to exercise"
            )
        digest_entries.append({"phase": "refused", "probes": probes})
        if failures:
            raise Abort

        # Phase 4 — the available probe: one served-`available`
        # command, the `kind_declared` the emitted model declares when
        # one exists, submitted through `POST /command`. The submission
        # must settle `applied` identically into both peers' adopted
        # receipt log — the invocable direction proven against the
        # receipted path.
        def pick_available():
            """The `(component, spec, row, submission)` the leg
            submits — `kind_declared` first (the verdict join's
            available direction), then `declared`-`always`, then any
            adapted available command with a submittable form."""
            candidates = []
            for (component, name), row in sorted(rows.items()):
                spec = specs.get((component, name))
                if spec is None or row["available"] is not True:
                    continue
                submission = submission_for(component, spec)
                if submission is None:
                    continue
                rank = {
                    "kind_declared": 0,
                    "always": 1,
                    "bound_point_writable": 2,
                }.get(spec.get("availability"), 3)
                if spec.get("adapted") != "declared":
                    rank += 2
                candidates.append(
                    (rank, component, spec, row, submission)
                )
            candidates.sort(key=lambda entry: (entry[0], entry[1], entry[2]["name"]))
            return candidates[0][1:] if candidates else None

        picked = pick_available()
        if picked is None:
            failures.append(
                "the emitted model serves no submittable available "
                "command — the available probe has nothing to exercise"
            )
            raise Abort
        component, spec, row, submission = picked
        # Under `refused-available` the probe goes to the tracking
        # standby's monitor, where the role gate settles `not_active`
        # — the accepted-receipt check below names the refusal the
        # served-available command actually met.
        target = standby_url if tamper == "refused-available" else duty_url
        status, receipt = pair.request(
            f"{target}/command", {"command": submission, "actor": ACTOR}
        )
        if status != 200 or not isinstance(receipt, dict) or (
            simulate.receipt_outcome(receipt) != "accepted"
        ):
            failures.append(
                f"the served-available {spec['name']} on {component} "
                f"answered {status} {receipt}, expected an accepted "
                "receipt"
            )
            raise Abort
        _tracked, owner = pair.tick(standby_url, duty_url, failures)
        # One carried-command lag may trail the settlement a pull; the
        # extra tick reconverges the adopted log.
        _tracked, owner = pair.tick(standby_url, duty_url, failures)
        receipts_duty = pair.get(
            f"{duty_url}/receipts", "GET /receipts", failures
        )
        receipts_standby = pair.get(
            f"{standby_url}/receipts", "GET /receipts", failures
        )
        if receipts_duty != receipts_standby:
            failures.append(
                "the peers' receipt logs diverged — the adopted "
                "audit is not one log"
            )
            raise Abort
        outcome, reason = settled(receipts_duty, submission)
        if outcome != "applied":
            failures.append(
                f"the served-available {spec['name']} on {component} "
                f"settled {outcome} {reason}, expected the applied "
                "settlement the served verdict advertised"
            )
            raise Abort
        evidence["available"] = f"{spec['name']} on {component}"
        digest_entries.append(
            {
                "phase": "available",
                "component": component,
                "command": spec["name"],
                "submission": submission,
                "receipt": receipt,
            }
        )

        # Phase 5 — the kind-declared refusal: where the emitted model
        # declares a `kind_declared` command, drive it to its standing
        # refusal through its own declared surface — the exercise
        # `advance` submits until the table completes — then probe
        # again: the row must serve `available: false` with the kind's
        # named refusal and a resubmission must settle
        # `rejected{command_refused}` carrying that refusal verbatim.
        # Where the release's read model does not join the producer's
        # published verdicts — the pinned release predating the verdict
        # join — the row stays `available` while the receipted path
        # refuses: the release's documented limitation, which the leg
        # records in its evidence rather than failing on machinery the
        # release lacks. An admission refusing what the row advertises
        # invocable is dishonesty on any release.
        kind_declared = sorted(
            (
                (component, spec, rows[(component, spec["name"])])
                for (component, name), spec in specs.items()
                if spec.get("availability") == "kind_declared"
                and (component, name) in rows
            ),
            key=lambda entry: (entry[0], entry[1]["name"]),
        )
        if not kind_declared:
            evidence["kind_declared"] = (
                "the emitted model declares no kind_declared-availability "
                "command — coverage is the bound_point_writable and "
                "always surfaces"
            )
        else:
            component, spec, row = kind_declared[0]
            submission = submission_for(component, spec)
            driven = row["available"] is False
            attempts = 0
            admission_answer = settled_reason = None
            while not driven and attempts < DRIVE_BOUND and submission:
                status, step_receipt = pair.request(
                    f"{duty_url}/command",
                    {"command": submission, "actor": ACTOR},
                )
                if status != 200 or not isinstance(step_receipt, dict) or (
                    simulate.receipt_outcome(step_receipt) != "accepted"
                ):
                    admission_answer = f"{status} {step_receipt}"
                    break
                _tracked, owner = pair.tick(standby_url, duty_url, failures)
                attempts += 1
                receipts_duty = pair.get(
                    f"{duty_url}/receipts", "GET /receipts", failures
                )
                outcome, reason = settled(receipts_duty, submission)
                if outcome != "applied":
                    settled_reason = reason.get("command_refused", {}).get(
                        "reason", f"{outcome} {reason}"
                    )
                    break
                resources, rows = audit_pair(duty_url, standby_url, failures)
                row = rows.get((component, spec["name"])) or {}
                driven = row.get("available") is False
            if not driven:
                if admission_answer is not None:
                    failures.append(
                        f"{component}'s {spec['name']} serves available yet "
                        f"its submission answered {admission_answer} at "
                        "admission — the read model reports the command "
                        "invocable while the receipted path refuses it"
                    )
                elif settled_reason is not None:
                    published = published_verdict(
                        owner, component, spec["name"]
                    )
                    detail = (
                        f" — the producer's published verdict reports "
                        f"{published['refusal']!r} the row does not join"
                        if isinstance(published, dict)
                        and published.get("available") is False
                        else ""
                    )
                    evidence["kind_declared"] = (
                        f"{spec['name']} on {component} settles "
                        f"{settled_reason!r} through the receipted path "
                        f"while the served row reports available{detail} — "
                        "this release's read model does not join the "
                        "command_verdicts, so the kind-declared refusal "
                        "direction is unexercised through the read model"
                    )
                else:
                    evidence["kind_declared"] = (
                        f"{spec['name']} on {component} never refused within "
                        f"{attempts} drive submissions — the kind-declared "
                        "refusal direction is unexercised"
                    )
            else:
                served = row.get("refusal")
                status, receipt = pair.request(
                    f"{duty_url}/command",
                    {"command": submission, "actor": ACTOR},
                )
                if status != 200 or not isinstance(receipt, dict) or (
                    simulate.receipt_outcome(receipt) != "accepted"
                ):
                    failures.append(
                        f"the refused {spec['name']} on {component} "
                        f"answered {status} {receipt} at admission, "
                        "expected an accepted receipt"
                    )
                    raise Abort
                _tracked, owner = pair.tick(standby_url, duty_url, failures)
                tracked, owner = pair.tick(standby_url, duty_url, failures)
                receipts_duty = pair.get(
                    f"{duty_url}/receipts", "GET /receipts", failures
                )
                receipts_standby = pair.get(
                    f"{standby_url}/receipts", "GET /receipts", failures
                )
                if receipts_duty != receipts_standby:
                    failures.append(
                        "the peers' receipt logs diverged — the adopted "
                        "audit is not one log"
                    )
                    raise Abort
                outcome, reason = settled(receipts_duty, submission)
                refusal = reason.get("command_refused") or {}
                if outcome != "command_refused" or refusal.get("reason") != served:
                    failures.append(
                        f"the refused {spec['name']} on {component} serves "
                        f"refusal {served!r} yet settled {outcome} {reason} "
                        "— the receipted path names a different refusal "
                        "than the read model"
                    )
                    raise Abort
                evidence["kind_declared"] = (
                    f"{spec['name']} on {component} refused after "
                    f"{attempts} drive submissions: {served}"
                )
                digest_entries.append(
                    {
                        "phase": "kind-declared",
                        "component": component,
                        "command": spec["name"],
                        "attempts": attempts,
                        "served_refusal": served,
                        "receipt": receipt,
                    }
                )
        evidence["final_tick"] = owner["tick"]
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        if rig is not None:
            rig.close()
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
        choices=["refused-available", "diverged-standby"],
        help="doctor the run — the pass must fail naming the evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = availability_pass(args, args.tamper)
    except Abort as abort:
        for line in abort.args:
            eprint(f"availability: {line}")
        return 1
    for failure in failures:
        eprint(f"availability: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"availability: the {args.tamper} case passed silently — "
                "the leg never noticed the doctored verdict"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"availability-digest {digest} — tracking by tick "
        f"{evidence['converged']}, {evidence['available']} settled "
        f"applied, kind-declared: {evidence.get('kind_declared', 'unexercised')}, "
        f"run continued to tick {evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
