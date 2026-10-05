#!/usr/bin/env python3
"""The injected field-fault degradation-and-recovery leg for the
reference plant — the consumer-owned mirror of the rig-side field-fault
leg (`qa_lane/scenarios/3600_field_fault.py`), on the deployed
redundant pair rather than on the lane's rig (WW-ENG-003,
WW-OPS-003).

The consumer pair's released tooling already exposes the unfenced
field-side fault surface (`ci/simulate.py`'s `PlantClient`
`inject_fault`/`clear_fault`, which the alarm-burst leg uses to *order*
an alarm cascade). No leg asserted the honest-degradation contract
those faults exist to exercise: a faulted field input must serve its
substituted quality — never a silently `Good` value — the transition
must be journaled, a disconnected-class fault must surface on the
I/O-health counters with its tick and direction rather than as a
process alarm or a role change, and clearing the faults must return the
simulated field values at `Good` with the recovery journaled
identically on both peers' adopted records.

The rig launches the released tooling exactly as the pair legs do —
`dcs-plant-server` serving the model and dynamics and the two
`dcs-controller --driven --remote` peers converged to
`active`/`tracking` — through the shared harness consolidated under
#647 (`ci/legs/pair.py`'s `launch_pair`/`PairRig`). The run:

- converges the declared standby to `tracking` through the pair leg's
  driven-tick loop and settles the pump group on a duty demand, so the
  simulated field values the recovery leg compares are the ones the
  dynamics assert, not a plant still filling;
- resolves the emitted model's declared field `In` points out of its
  signal index (`level-primary`, `level-backup`) rather than naming
  hard-coded ids, so the leg exercises the declared seam;
- injects a **quality** fault on the first through the plant protocol's
  unfenced fault surface and drives tracking-first ticks until the
  active's served snapshot serves that faulted quality — never `Good`,
  which would be a silently healthy field input — with the quality
  transition journaled on the owner's durable record;
- injects a **disconnected-class** fault on the second and drives
  ticks until `io_health`'s `failed_reads` advances: the boundary
  fault must be counted with its tick and direction in `last_error`,
  the scan must continue (the run's tick advancing, the point quality
  degrading rather than the process stopping), and **no role may
  change** on either peer — a field fault is not a peer failure;
- clears both faults and drives ticks until the simulated field values
  return at `Good`, with the recovery transitions journaled
  identically on both peers' durable records and the same transition
  visible through each peer's served journal;
- restores the pair's launch roles for the next leg and leaves the
  launch posture unchanged.

A served snapshot carrying no `io_health`, a fault surface that cannot
land, or a field the run cannot fault is inconclusive — the pinned
release predating the contract or the rig failing its precondition,
never a product failure.

Usage:

    field_fault.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `field-fault-digest <sha256>` line prints — the check
runs two passes and compares them (`field-fault-nondeterministic`). A
contract violation reports `field-fault: …` lines on stderr and exits
1 — the check's `field-fault-failed`. The three doctored cases each
fail carrying their named evidence: `--tamper serve-good` reads the
faulted point as the substituted quality were not served at all,
`--tamper flat-counters` reads the disconnected-class fault as though
no boundary failed, and `--tamper lingering-quality` reads the cleared
field as though the substituted quality still stood.
"""

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import pair
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its two
# digest-identical passes, and exercise its doctored cases. The three
# doctored cases are the leg's own three contract clauses: a faulted
# point that still serves Good, a disconnected-class fault whose
# counters never moved, and a cleared field whose substituted quality
# never lifted — a leg whose own audit is never proven to fire cannot
# register.
LEG = {
    "order": 135,
    "title": "the injected field-fault leg",
    "passes": "field-fault",
    "tampers": [
        {
            "name": "serve-good",
            "passed": "a serve-good case passed the field-fault leg",
            "missed": "the serve-good case did not report its named diagnostic",
            "evidence": ["a silently healthy measurement"],
        },
        {
            "name": "flat-counters",
            "passed": "a flat-counters case passed the field-fault leg",
            "missed": "the flat-counters case did not report its named diagnostic",
            "evidence": ["must be counted at its boundary"],
        },
        {
            "name": "lingering-quality",
            "passed": "a lingering-quality case passed the field-fault leg",
            "missed": "the lingering-quality case did not report its named diagnostic",
            "evidence": ["must return the point to Good"],
        },
    ],
}


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The pinned release predates the contract the leg exercises, or a
    staging lever never landed — the run classifies inconclusive, never
    a product failure."""


# The driven-tick bounds each phase gets: the demand's arrival, each
# fault's transition, and the recovery's return. The carried-point seam
# crosses one hop per scan, so each injected cause's declared effect
# takes a few scans to arrive; the bound names a transition that never
# came.
DEMAND_BOUND = 24
TRANSITION_BOUND = 12
RECOVERY_BOUND = 16

#: The quality the fault surface substitutes — the same class the burst
#: leg's cascade is ordered by, named here as this leg's own subject.
BAD_QUALITY = {"bad": "device_fault"}

#: The signal names resolving the leg's field inputs out of the emitted
#: model — the declared seam, never hard-coded point ids.
SIGNALS = {
    "level_primary": "level-primary",
    "level_backup": "level-backup",
    "demand": "demand",
    "staged": "staged",
    "run_1": "p101-run",
    "run_2": "p102-run",
    "avail_1": "p101-avail",
    "avail_2": "p102-avail",
}


def signal_points(model):
    """The `{key: point id}` map the emitted model's signal index
    declares for the leg's field inputs — None when the model declares
    no such set. A signal's `source` is the point it names; the
    lowest-signal-id-wins rule the served index applies."""
    by_name = {}
    for signal in model.get("signals", []):
        current = by_name.get(signal["name"])
        if current is None or signal["id"] < current[0]:
            by_name[signal["name"]] = (signal["id"], signal["source"])
    points = {}
    for key, signal_name in SIGNALS.items():
        entry = by_name.get(signal_name)
        points[key] = entry[1] if entry else None
    if any(point is None for point in points.values()):
        return None
    return points


def value(snapshot, point):
    """The point's latest sample value — `simulate.snapshot_point`."""
    return simulate.snapshot_point(snapshot, point)


def quality(snapshot, point):
    """The point's latest served quality — `Good` for a snapshot
    carrying no sample, which the callers below treat as unread."""
    for entry in (snapshot or {}).get("points") or []:
        if entry.get("point") == point and isinstance(entry.get("sample"), dict):
            return entry["sample"].get("quality")
    return None


def health(snapshot):
    """The served `io_health` section, or None when the release serves
    none — the contract's own surface, and its absence a pinned release
    predating it."""
    section = (snapshot or {}).get("io_health")
    return section if isinstance(section, dict) else None


def counter(section, name):
    """One `io_health` counter as an int, or None — a section carrying
    no integer where the contract reads one is unread, never zero."""
    got = section.get(name) if isinstance(section, dict) else None
    return got if isinstance(got, int) and not isinstance(got, bool) else None


def tick(rig, failures):
    """One driven pair tick — the tracking peer scanned first so its
    pull applies the owner's latest checkpoint, then the field owner."""
    _tracked, owner = rig.tick(rig.standby_url, rig.duty_url, failures)
    return owner


def drive_until(rig, failures, condition, bound=TRANSITION_BOUND):
    """Driven pair ticks until `condition(owner_snapshot)` holds —
    returns the satisfying snapshot, or None when `bound` scans pass
    without it landing."""
    for _ in range(bound):
        owner = tick(rig, failures)
        if condition(owner):
            return owner
    return None


def journal_events(entries):
    """The leg's audit stream out of a journal entry list —
    `("changed", point, to)` per journaled value transition and
    `("quality", point, to)` per quality transition — in `seq` order."""
    events = []
    for entry in entries or []:
        event = entry.get("event", {})
        if "point_changed" in event:
            change = event["point_changed"]
            events.append(("changed", change.get("point"), change.get("to")))
        elif "quality_changed" in event:
            change = event["quality_changed"]
            events.append(("quality", change.get("point"), change.get("to")))
    return events


def durable_events(rig, files):
    """The durable `role`/`journal` entries a peer's declared
    `--journal-file` carries, read host-side from the runner-owned
    scratch the pair rig instantiated them under."""
    events = {}
    for key, path in files.items():
        records = [record for kind, record in pair.journal_records(path)
                   if kind == "entry"]
        events[key] = journal_events(records)
    return events


def roles_hold(rig, failures, when):
    """One launch-roles poll — the field owner `active`, the tracking
    peer `standby`/`tracking`. A moved role is a contract violation,
    not a note: a field fault is not a peer failure. Returns the two
    role reports."""
    duty_role = pair.get(f"{rig.duty_url}/role", "GET /role", failures)
    standby_role = pair.get(
        f"{rig.standby_url}/role", "GET /role", failures
    )
    sync = standby_role.get("sync")
    held = duty_role.get("role") == "active" and (
        standby_role.get("role") == "standby"
        and isinstance(sync, dict)
        and "tracking" in sync
    )
    if not held:
        failures.append(
            f"the pair's launch roles did not hold {when} — the field "
            f"owner reports {json.dumps(duty_role)[:200]} and the "
            f"tracking peer {json.dumps(standby_role)[:200]}"
        )
    return duty_role, standby_role


def fault_pass(args, tamper):
    """The field-fault run: converge, demand, quality fault, read the
    degraded serve and its journal, disconnected fault, read the
    counters, clear both, read the recovery and both peers' records.
    Returns `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the field-fault leg "
            "has nothing to exercise"
        )
    with open(args.model) as handle:
        model = json.load(handle)
    points = signal_points(model)
    if points is None:
        raise Abort(
            "the emitted model declares no station level set — the "
            "field-fault leg has nothing to exercise"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared, tamper)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        duty_files = rig.duty_files
        standby_files = rig.standby_files
        plant_io = rig.plant_io

        # Phase 1 — convergence through the pair leg's driven-tick
        # loop, then the healthy precondition: the pump group holding a
        # full demand, so the recovery half compares the values the
        # dynamics assert.
        converged = rig.converge(failures)
        owner = converged["owner"]
        if health(owner) is None:
            raise Inconclusive(
                "the served snapshot carries no io_health section — the "
                "pinned release predates the I/O-health surface this "
                "leg reads"
            )
        evidence["converged"] = converged["ticks"][-1]
        digest_entries.append(
            {"phase": "converge", "ticks": converged["ticks"]}
        )
        held = None
        for _ in range(DEMAND_BOUND):
            owner = tick(rig, failures)
            staged = value(owner, points["staged"]) or {}
            if (
                staged.get("int", 0) >= 1
                and value(owner, points["run_1"]) == {"bool": True}
                and value(owner, points["avail_1"]) == {"bool": True}
            ):
                held = owner
                break
        if held is None:
            failures.append(
                "the pump group never held a demand — the field-fault "
                "leg's healthy precondition never arrived"
            )
            raise Abort
        evidence["demand_at"] = held["tick"]
        digest_entries.append(
            {"phase": "demand", "tick": held["tick"],
             "staged": value(held, points["staged"])}
        )
        # The two field inputs this leg faults, and the qualities they
        # carry while healthy. The *values* are the dynamics' to move —
        # a level keeps tracking the well's inflow and the pumps across
        # the fault window — so the recovery compares qualities, not a
        # sample the plant was always going to leave behind.
        healthy = {
            key: quality(owner, points[key])
            for key in ("level_primary", "level_backup")
        }
        digest_entries.append({"phase": "healthy", "qualities": healthy})

        # Phase 2 — the quality fault: the active's monitor must serve
        # the substituted quality, never a silently Good value, and
        # the transition must be journaled on the owner's record.
        verdict = plant_io.request(
            {
                "op": "inject_fault",
                "point": points["level_primary"],
                "fault": {"quality": BAD_QUALITY},
            }
        )
        if verdict.get("result") != "done":
            failures.append(
                "inject_fault on level-primary answered "
                f"{json.dumps(verdict)[:200]}"
            )
            raise Abort
        degraded = drive_until(
            rig, failures,
            lambda snapshot: quality(snapshot, points["level_primary"])
            == BAD_QUALITY,
        )
        if degraded is not None:
            owner = degraded
        served = quality(owner, points["level_primary"])
        if tamper == "serve-good":
            # The doctored read: the faulted point answers as though the
            # fault had never been injected.
            served = "good"
        if served != BAD_QUALITY:
            failures.append(
                "the quality fault on level-primary never surfaced: the "
                f"active serves {json.dumps(served)[:200]}, expected "
                f"{json.dumps(BAD_QUALITY)} — a faulted field input "
                "serving Good is a silently healthy measurement, not a "
                "degraded one"
            )
            raise Abort
        evidence["degraded_at"] = owner["tick"]
        digest_entries.append(
            {"phase": "degraded", "tick": owner["tick"],
             "quality": served}
        )
        events = durable_events(rig, {"duty": duty_files.get("journal_file"),
                                      "standby":
                                      standby_files.get("journal_file")})
        journaled = any(
            event == ("quality", points["level_primary"], BAD_QUALITY)
            for event in events.get("duty") or []
        )
        if not journaled:
            failures.append(
                "the quality transition was never journaled on the "
                "field owner's durable record — the faulted field "
                "input's degraded serve left no trace: "
                f"{json.dumps(events.get('duty'))[:400]}"
            )
            raise Abort
        digest_entries.append(
            {"phase": "degraded-journal", "duty": events.get("duty")}
        )

        # Phase 3 — the disconnected-class fault: the boundary failure
        # must surface on the I/O-health counters with its tick and
        # direction, the scan must continue, and no role may move.
        before = health(owner) or {}
        verdict = plant_io.request(
            {
                "op": "inject_fault",
                "point": points["level_backup"],
                "fault": "disconnected",
            }
        )
        if verdict.get("result") != "done":
            failures.append(
                "inject_fault on level-backup answered "
                f"{json.dumps(verdict)[:200]}"
            )
            raise Abort
        broke = drive_until(
            rig, failures,
            lambda snapshot: (counter(health(snapshot) or {},
                                      "failed_reads") or 0)
            > (counter(before, "failed_reads") or 0),
        )
        if broke is None:
            owner = tick(rig, failures)
        else:
            owner = broke
        section = health(owner) or {}
        if tamper == "flat-counters":
            # The doctored read: the boundary failure never counted.
            section = {**section, "failed_reads": counter(before,
                                                          "failed_reads") or 0}
        failed_reads = counter(section, "failed_reads")
        advanced = failed_reads is not None and failed_reads > (
            counter(before, "failed_reads") or 0
        )
        if not advanced:
            failures.append(
                "the disconnected-class fault on level-backup never "
                "advanced io_health's failed_reads — a field read that "
                "fails must be counted at its boundary: "
                f"{json.dumps(section)[:300]}"
            )
            raise Abort
        last_error = section.get("last_error")
        if not isinstance(last_error, dict) or not last_error.get("error"):
            failures.append(
                "the counted boundary carries no last_error naming the "
                "failure — the counter says how many, never which: "
                f"{json.dumps(last_error)[:300]}"
            )
            raise Abort
        if last_error.get("tick") is None or last_error.get("direction") is None:
            failures.append(
                "the boundary's last_error must carry its tick and "
                "direction, so an operator can attribute the failure: "
                f"{json.dumps(last_error)[:300]}"
            )
            raise Abort
        # The scan continues: the run's tick advances across the outage
        # and the faulted point degrades rather than the run stopping.
        after = tick(rig, failures)
        if after["tick"] <= owner["tick"]:
            failures.append(
                "the run stopped scanning under the field fault — the "
                f"active's tick stands at {after['tick']} across the "
                "faulted boundary"
            )
            raise Abort
        roles_hold(rig, failures, "under the disconnected-class fault")
        evidence["broken_at"] = after["tick"]
        digest_entries.append(
            {"phase": "disconnected", "tick": after["tick"],
             "failed_reads": failed_reads,
             "consecutive_failures": counter(section,
                                             "consecutive_failures"),
             "last_error": last_error}
        )

        # Phase 4 — the recovery: clearing both faults returns the
        # simulated field values at Good, and the recovery is journaled
        # identically on both peers' durable records.
        for key in ("level_primary", "level_backup"):
            verdict = plant_io.request(
                {"op": "clear_fault", "point": points[key]}
            )
            if verdict.get("result") != "done":
                failures.append(
                    f"clear_fault on {key} answered "
                    f"{json.dumps(verdict)[:200]}"
                )
                raise Abort
        recovered = drive_until(
            rig, failures,
            lambda snapshot: all(
                quality(snapshot, points[key]) == "good"
                for key in ("level_primary", "level_backup")
            ),
            bound=RECOVERY_BOUND,
        )
        if recovered is None:
            owner = tick(rig, failures)
        else:
            owner = recovered
        standing = {
            key: {
                "value": value(owner, points[key]),
                "quality": quality(owner, points[key]),
            }
            for key in ("level_primary", "level_backup")
        }
        if tamper == "lingering-quality":
            # The doctored read: the substituted quality never lifted.
            standing = {
                key: dict(sample, quality=BAD_QUALITY)
                for key, sample in standing.items()
            }
        stale = sorted(
            key for key, sample in standing.items()
            if sample["quality"] != "good"
        )
        if stale:
            failures.append(
                f"the cleared field still serves a substituted quality "
                f"on {json.dumps(stale)} — a fault cleared through the "
                "field's own surface must return the point to Good: "
                f"{json.dumps(standing)[:300]}"
            )
            raise Abort
        # The recovered points serve the field's own current values: a
        # finite float the plant is still driving, never a stand-in the
        # fault left behind.
        for key, sample in standing.items():
            reading = sample["value"]
            if not isinstance(reading, dict) or not isinstance(
                reading.get("float"), (int, float)
            ) or isinstance(reading.get("float"), bool):
                failures.append(
                    f"the recovered {key} serves "
                    f"{json.dumps(reading)}, not a simulated field value "
                    "the plant is still driving — the clear returned a "
                    "stand-in rather than the field's own reading"
                )
                raise Abort
        evidence["recovered_at"] = owner["tick"]
        digest_entries.append(
            {"phase": "recovered", "tick": owner["tick"],
             "samples": standing}
        )

        # The recovery's transitions are journaled identically on both
        # peers' durable records — the tracking peer's adopted journal
        # shows the same quality returns the owner's own record carries.
        recovered_events = durable_events(
            rig, {"duty": duty_files.get("journal_file"),
                  "standby": standby_files.get("journal_file")}
        )
        returns = [
            ("quality", points[key], "good")
            for key in ("level_primary", "level_backup")
        ]
        duty_returns = [event for event in recovered_events.get("duty") or []
                        if event in returns]
        standby_returns = [event for event in
                           recovered_events.get("standby") or []
                           if event in returns]
        if duty_returns != standby_returns:
            failures.append(
                "the recovery's journaled quality returns differ "
                "between the two peers' durable records — the tracking "
                "peer must adopt the owner's record: "
                f"duty {json.dumps(duty_returns)[:300]} versus standby "
                f"{json.dumps(standby_returns)[:300]}"
            )
            raise Abort
        if not duty_returns:
            failures.append(
                "the recovery's quality returns were never journaled: "
                f"{json.dumps(recovered_events.get('duty'))[:400]}"
            )
            raise Abort
        digest_entries.append(
            {"phase": "recovered-journal",
             "duty": duty_returns, "standby": standby_returns}
        )

        # The served journal answers the same record each durable file
        # carries, and the launch roles still hold at the end.
        served_events = journal_events(
            pair.get(f"{duty_url}/journal", "GET /journal", failures)
        )
        if served_events != recovered_events.get("duty"):
            failures.append(
                "the served journal's transition stream diverges from "
                "the durable file's — the monitor does not answer the "
                "record it persists"
            )
            raise Abort
        roles_hold(rig, failures, "after the field-fault recovery")
        digest_entries.append(
            {"phase": "audit",
             "duty": quality_events(recovered_events, "duty"),
             "standby": quality_events(recovered_events, "standby")}
        )
        evidence["final_tick"] = owner["tick"]
    except Inconclusive as inconclusive:
        failures.append(f"field-fault-inconclusive: {inconclusive}")
    except Abort as abort:
        failures.extend(str(argument) for argument in abort.args)
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        if rig is not None:
            rig.close()
    return digest_entries, evidence, failures


def quality_events(events, key):
    """The quality transitions a peer's durable record carries — the
    stream the recovery comparison reads."""
    return [event for event in events.get(key) or []
            if event[0] == "quality"]


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
        choices=["serve-good", "flat-counters", "lingering-quality"],
        help="doctor the read the audit inspects — the pass must fail "
        "naming the honest-degradation clause it broke",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = fault_pass(args, args.tamper)
    except Abort as abort:
        for line in abort.args:
            eprint(f"field-fault: {line}")
        return 1
    for failure in failures:
        if failure.startswith("field-fault-inconclusive"):
            eprint(failure)
            return 0
        eprint(f"field-fault: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"field-fault: the {args.tamper} case passed silently "
                "— the leg never noticed the doctored read"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"field-fault-digest {digest} — tracking by tick "
        f"{evidence['converged']}, demand at tick {evidence['demand_at']}, "
        f"degraded at tick {evidence['degraded_at']}, the boundary "
        f"failure at tick {evidence['broken_at']}, recovered at tick "
        f"{evidence['recovered_at']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
