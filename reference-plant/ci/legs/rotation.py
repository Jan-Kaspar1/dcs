#!/usr/bin/env python3
"""The duty-rotation carryover leg for the reference plant — the
consumer-side proof that the emitted model's declared duty-rotation
policy holds on the deployed redundant pair, and that a promotion
carries the rotation position and accumulated run-hours rather than
resetting them (WW-ENG-003, WW-CTL-001, WW-LCM-001's
runtime-state-continuity clause).

The pair leg (`ci/legs/pair.py`) proves the manifest-declared pair runs
and switches; the handover leg proves a proven duty-pump failure hands
`duty` to the standby pump. What no leg proves is the `pump-group`'s
declared `rotation` policy — `rotation = 0`, alternate each cycle —
nor WW-LCM-001's station-facing acceptance naming duty-rotation
position and accumulated run-hours among the run state a takeover
carries. The rig reads the standby wiring and persistence fields out
of `deploy/manifest.json` and spawns the released tooling exactly as
the pair legs do. The run:

- converges the declared standby to `tracking` through the pair leg's
  driven-tick loop, then drives the simulated plant through its
  natural demand cycles — the declared inflow raising the well to
  `start`, the staged pumps drawing it back to `stop`;
- asserts the first demand cycle designates `duty` to pump 1 and the
  group's served checkpoint shows run-hours accruing for the running
  pump, the cycle-end rotation moving the designation to pump 2;
- asserts the second cycle honors the declared `min_off_ticks`
  holdout — the just-released pump cannot re-stage inside its banked
  bound — while the duty designation and the covered demand stand;
- demotes the field owner and promotes the converged standby
  mid-cycle, asserting on the promoted peer that the pump-group's
  checkpointed state carried — `duty`, `rotation_cursor`, and the
  per-pump `run_hours` never reset, the commanded run continuing
  bumplessly through the switch;
- restores the pair's roles and drives a third cycle, asserting the
  rotation resumes its alternation on the peer that took the field —
  the designation returning to pump 1 — the promotion having carried
  the rotation position rather than restarted it;
- audits the record: each peer's served journal carries the switch's
  role transitions, the adopted receipt logs stay one log, and every
  transition lands in the run's `seq` order.

Usage:

    rotation.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `rotation-digest <sha256>` line prints — the check
runs two passes and compares them (`rotation-nondeterministic`). A
contract violation reports `rotation: …` lines on stderr and exits 1
— the check's `rotation-failed`. The `--tamper` cases doctor the
leg's own expectations: `resets-duty` requires the promoted peer to
report `duty` reset to pump 1, and `fresh-hours` requires the
promoted peer's run-hours to read zero — each must fail naming the
carried evidence.
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
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored cases: a leg asserting the promoted peer reset the
# duty designation to pump 1, or restarted the run-hours at zero,
# must surface the named diagnostic — the carried state must fail
# them.
LEG = {
    "order": 840,
    "title": "the duty-rotation carryover leg",
    "passes": "rotation-leg",
    "tampers": [
        {
            "name": "resets-duty",
            "passed": "a doctored duty-reset expectation passed the rotation leg",
            "missed": "the resets-duty case did not report its named diagnostic",
            "evidence": ["duty reset to pump 1"],
        },
        {
            "name": "fresh-hours",
            "passed": "a doctored hours-reset expectation passed the rotation leg",
            "missed": "the fresh-hours case did not report its named diagnostic",
            "evidence": ["run-hours reset"],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The driven ticks each phase's bound allows — the duty-demand wait,
# each demand cycle's full fill-and-draw, and the carrier-hop bound
# the post-switch assertions land across.
DEMAND_BOUND = 24
CYCLE_BOUND = 40

# The signal names resolving the leg's point ids out of the emitted
# model — the same names the signal index serves, so the leg speaks
# the declared seam, never hard-coded ids.
SIGNALS = {
    "demand": "demand",
    "duty": "duty",
    "staged": "staged",
    "cmd_1": "p101-cmd",
    "run_1": "p101-run",
    "cmd_2": "p102-cmd",
    "run_2": "p102-run",
}


def signal_points(model):
    """The `{key: point id}` map the emitted model's signal index
    declares for the leg's rotation seam — None when the model
    declares no such seam."""
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


def pump_group(model):
    """The emitted model's `pump-group` component's served name and
    declared `min_off_ticks`/`start_delay_ticks` bounds — the leg's
    rotation and holdout evidence resolved out of the model, never
    assumed."""
    for component in model.get("components", []):
        if component.get("kind") != "pump-group":
            continue
        parameters = component.get("parameters", {})
        min_off = parameters.get("min_off_ticks", {}).get("int")
        start_delay = parameters.get("start_delay_ticks", {}).get("int")
        rotation = parameters.get("rotation", {}).get("int")
        if None not in (min_off, start_delay, rotation):
            return (
                f"{component['kind']}:{component['id']}",
                min_off,
                start_delay,
                rotation,
            )
    return None


def value(snapshot, point):
    """The point's latest sample value — `simulate.snapshot_point`."""
    return simulate.snapshot_point(snapshot, point)


def flag(snapshot, point):
    """The point's latest Bool sample value, or None."""
    reading = value(snapshot, point)
    return reading.get("bool") if isinstance(reading, dict) else None


def group_state(checkpoint, component):
    """One component's checkpointed field map — `pump-group:N`'s
    `duty`/`rotation_cursor`/`run_hours_*`/`commanded_*`/`held_until_*`
    as the served checkpoint carries them, or None while absent."""
    entry = (checkpoint.get("components") or {}).get(component)
    if not isinstance(entry, dict):
        return None
    fields = entry.get("fields")
    return fields if isinstance(fields, dict) else None


def int_field(fields, name):
    """One checkpointed field's Int value, or None."""
    reading = (fields or {}).get(name)
    return reading.get("int") if isinstance(reading, dict) else None


def bool_field(fields, name):
    """One checkpointed field's Bool value, or None."""
    reading = (fields or {}).get(name)
    return reading.get("bool") if isinstance(reading, dict) else None


def checkpoint(url, failures):
    """The peer's served `GET /checkpoint`."""
    return pair.get(f"{url}/checkpoint", "GET /checkpoint", failures)


def tick(rig, failures):
    """One driven pair tick — the harness's tracking-first scan,
    identical images asserted — returns the owner's served
    snapshot."""
    return rig.tick(rig.standby_url, rig.duty_url, failures)[1]


def drive_until(rig, failures, condition, bound=CYCLE_BOUND):
    """Driven pair ticks until `condition(owner_snapshot)` holds —
    returns the satisfying snapshot, or None when `bound` scans pass
    without it landing."""
    for _ in range(bound):
        owner = tick(rig, failures)
        if condition(owner):
            return owner
    return None


def rotation_pass(args, tamper):
    """The rotation run: converge, two demand cycles, mid-cycle
    promotion with carried state, restore, third cycle, audit.
    Returns `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the rotation leg "
            "has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    with open(args.model) as handle:
        model = json.load(handle)
    points = signal_points(model)
    if points is None:
        raise Abort(
            "the emitted model declares no pump-group duty/demand "
            "signals — the rotation leg has nothing to exercise"
        )
    group = pump_group(model)
    if group is None:
        raise Abort(
            "the emitted model declares no pump-group rotation "
            "parameters — the rotation leg has nothing to exercise"
        )
    component, min_off, _start_delay, rotation_policy = group

    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared, tamper)
        duty_url, standby_url = rig.duty_url, rig.standby_url

        # Phase 1 — convergence: the pair leg's driven-tick loop, the
        # tracking peer scanned first so each pull applies the owner's
        # latest checkpoint and the peers rest identical.
        converged = rig.converge(failures)
        owner = converged["owner"]
        evidence["converged"] = converged["ticks"][-1]
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )

        # Phase 2 — the first demand cycle: the declared inflow raises
        # the well until the group holds a duty demand. The declared
        # policy assigns the designation in rotation order — the first
        # cycle names pump 1 — with run-hours accruing on the running
        # pump's checkpointed state.
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: value(snapshot, points["demand"]).get("int", 0)
            >= 1
            and value(snapshot, points["duty"]) == {"int": 1},
            bound=DEMAND_BOUND,
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                f"the first demand cycle never designated duty to pump "
                f"1 — demand reads {value(owner, points['demand'])}, "
                f"duty {value(owner, points['duty'])}"
            )
            raise Abort
        fields = group_state(checkpoint(duty_url, failures), component)
        if fields is None:
            failures.append(
                f"the served checkpoint carries no {component} state — "
                "the rotation leg's carryover evidence is absent"
            )
            raise Abort
        first_cycle = {
            "tick": owner["tick"],
            "demand": value(owner, points["demand"]),
            "duty": value(owner, points["duty"]),
            "staged": value(owner, points["staged"]),
            "state": fields,
        }
        evidence["first_cycle_at"] = owner["tick"]
        digest_entries.append({"phase": "first-cycle", **first_cycle})

        # The cycle's end: the staged pumps draw the well to `stop`,
        # the demand releases, and the declared policy rotates the
        # designation — the cycle ended handing `duty` to pump 2. The
        # designation moves on the scan after the demand falls, so the
        # wait is for both.
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: value(snapshot, points["demand"]).get("int", 0)
            == 0
            and value(snapshot, points["duty"]) == {"int": 2},
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                f"the declared alternate-each-cycle rotation did not "
                f"move the designation — demand reads "
                f"{value(owner, points['demand'])}, duty "
                f"{value(owner, points['duty'])}, expected duty pump 2"
            )
            raise Abort
        fields = group_state(checkpoint(duty_url, failures), component)
        if int_field(fields, "run_hours_1") in (None, 0):
            failures.append(
                f"the first cycle left run_hours_1 at "
                f"{int_field(fields, 'run_hours_1')} — the running "
                "pump's hours never accrued"
            )
            raise Abort
        # The just-released duty pump's banked holdout — the declared
        # `min_off_ticks` bound it cannot re-stage inside.
        held_until_1 = int_field(fields, "held_until_1")
        release_tick = owner["tick"]
        evidence["first_cycle_ended"] = release_tick
        digest_entries.append(
            {
                "phase": "first-end",
                "tick": release_tick,
                "duty": value(owner, points["duty"]),
                "state": fields,
            }
        )

        # Phase 3 — the second cycle: `duty` names pump 2 per the
        # declared rotation and covers the demand — the wait is for
        # the designated pump's command across its declared
        # start-delay. The holdout bound is honored — the just-
        # released pump cannot re-stage inside `min_off_ticks` — and
        # run-hours keep accruing on whichever pump the group stages.
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: value(snapshot, points["demand"]).get("int", 0)
            >= 1
            and flag(snapshot, points["cmd_2"]) is True,
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                f"the second demand cycle never staged the rotated "
                f"duty pump — demand reads "
                f"{value(owner, points['demand'])}, p102-cmd "
                f"{value(owner, points['cmd_2'])}"
            )
            raise Abort
        if value(owner, points["duty"]) != {"int": 2}:
            failures.append(
                f"the second cycle's duty designation reads "
                f"{value(owner, points['duty'])}, expected pump 2 — "
                "the declared rotation names the next pump in order"
            )
            raise Abort
        if (
            held_until_1 is not None
            and flag(owner, points["cmd_1"]) is True
            and owner["tick"] < held_until_1
        ):
            failures.append(
                f"pump 1 re-staged at tick {owner['tick']} inside its "
                f"banked holdout {held_until_1} — the declared "
                f"min_off_ticks {min_off} bound is not honored"
            )
            raise Abort
        fields = group_state(checkpoint(duty_url, failures), component)
        carried = {
            "tick": owner["tick"],
            "duty": int_field(fields, "duty"),
            "rotation_cursor": int_field(fields, "rotation_cursor"),
            "run_hours_1": int_field(fields, "run_hours_1"),
            "run_hours_2": int_field(fields, "run_hours_2"),
            "rotation_policy": rotation_policy,
        }
        evidence["second_cycle_at"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "second-cycle",
                **carried,
                "cmd_1": value(owner, points["cmd_1"]),
                "cmd_2": value(owner, points["cmd_2"]),
            }
        )

        # Phase 4 — the mid-cycle promotion: demote the field owner,
        # promote the converged standby while the demand still stands.
        # On the promoted peer the pump-group's checkpointed state must
        # carry — `duty`, `rotation_cursor`, and the per-pump
        # `run_hours` never reset — the commanded run continuing
        # bumplessly through the switch.
        promoted_state = {}

        def capture_promoted():
            fields = group_state(
                checkpoint(standby_url, failures), component
            )
            promoted_state["fields"] = fields

        switched = rig.switch(
            duty_url,
            standby_url,
            failures,
            audit_receipts=True,
            after_promote=capture_promoted,
        )
        fields = promoted_state.get("fields")
        if fields is None:
            failures.append(
                f"the promoted peer's checkpoint carries no "
                f"{component} state — the carryover evidence is absent"
            )
            raise Abort
        if tamper == "resets-duty":
            if int_field(fields, "duty") != 1:
                failures.append(
                    f"the promoted peer carried duty "
                    f"{int_field(fields, 'duty')} — the doctored leg "
                    "expected duty reset to pump 1"
                )
        elif int_field(fields, "duty") != 2:
            failures.append(
                f"the promoted peer reports duty "
                f"{int_field(fields, 'duty')}, expected the carried "
                "designation pump 2 — the promotion reset the rotation "
                "position"
            )
        if tamper == "fresh-hours":
            if (
                int_field(fields, "run_hours_1") != 0
                or int_field(fields, "run_hours_2") != 0
            ):
                failures.append(
                    f"the promoted peer carried run-hours "
                    f"{int_field(fields, 'run_hours_1')}/"
                    f"{int_field(fields, 'run_hours_2')} — the doctored "
                    "leg expected a run-hours reset to zero"
                )
        else:
            for name in ("run_hours_1", "run_hours_2"):
                carried_hours = int_field(fields, name)
                before = carried.get(name)
                if carried_hours is None or (
                    before is not None and carried_hours < before
                ):
                    failures.append(
                        f"the promoted peer's {name} reads "
                        f"{carried_hours} against the pre-switch "
                        f"{before} — the run-hours never carried"
                    )
            if int_field(fields, "rotation_cursor") != carried.get(
                "rotation_cursor"
            ):
                failures.append(
                    f"the promoted peer's rotation_cursor reads "
                    f"{int_field(fields, 'rotation_cursor')}, the "
                    f"pre-switch run carried "
                    f"{carried.get('rotation_cursor')} — the rotation "
                    "position did not carry"
                )
        if not (
            bool_field(fields, "commanded_1")
            or bool_field(fields, "commanded_2")
        ):
            failures.append(
                "the promoted peer commands no pump — the running "
                "machine did not continue bumplessly through the "
                "promotion"
            )
        if failures:
            raise Abort
        evidence["promoted_at"] = switched["promote"].get("tick")
        digest_entries.append(
            {
                "phase": "promote",
                "demote": switched["demote"],
                "promote": switched["promote"],
                "ticks": switched["ticks"],
                "carried": {
                    "duty": int_field(fields, "duty"),
                    "rotation_cursor": int_field(
                        fields, "rotation_cursor"
                    ),
                    "run_hours_1": int_field(fields, "run_hours_1"),
                    "run_hours_2": int_field(fields, "run_hours_2"),
                    "commanded_1": bool_field(fields, "commanded_1"),
                    "commanded_2": bool_field(fields, "commanded_2"),
                },
            }
        )

        # Phase 5 — the roles restored: the manifest-declared duty
        # controller takes the field back, the rotation position
        # carrying again — a second bumpless switch.
        restored = rig.switch(
            standby_url,
            duty_url,
            failures,
            audit_receipts=True,
        )
        digest_entries.append(
            {
                "phase": "restore",
                "demote": restored["demote"],
                "promote": restored["promote"],
                "ticks": restored["ticks"],
            }
        )

        # Phase 6 — the third cycle: the resumed alternation — the
        # designation returning to pump 1 on the peer that took the
        # field back, the run-hours still accruing.
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: value(snapshot, points["demand"]).get("int", 0)
            == 0,
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                f"the second demand cycle never ended — demand reads "
                f"{value(owner, points['demand'])}"
            )
            raise Abort
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: value(snapshot, points["demand"]).get("int", 0)
            >= 1
            and value(snapshot, points["duty"]) == {"int": 1},
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                f"the rotation never resumed its alternation after "
                f"the promotion — demand reads "
                f"{value(owner, points['demand'])}, duty "
                f"{value(owner, points['duty'])}, expected duty pump 1"
            )
            raise Abort
        fields = group_state(checkpoint(duty_url, failures), component)
        if int_field(fields, "run_hours_2") in (None, 0):
            failures.append(
                f"the promoted line left run_hours_2 at "
                f"{int_field(fields, 'run_hours_2')} — the accrued "
                "hours never carried"
            )
            raise Abort
        evidence["third_cycle_at"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "third-cycle",
                "tick": owner["tick"],
                "demand": value(owner, points["demand"]),
                "duty": value(owner, points["duty"]),
                "staged": value(owner, points["staged"]),
                "state": fields,
            }
        )

        # Phase 7 — the audit: the pair's roles rest as launched, each
        # peer's served journal carrying the switches' role
        # transitions the rig audited, and no other role movement.
        standby_role = pair.get(f"{standby_url}/role", "GET /role", failures)
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        sync = standby_role.get("sync")
        if standby_role.get("role") != "standby" or not (
            isinstance(sync, dict) and "tracking" in sync
        ):
            failures.append(
                f"the tracking peer's role never restored — GET /role "
                f"answers {standby_role}"
            )
        if duty_role.get("role") != "active":
            failures.append(
                f"the field owner reports {duty_role.get('role')!r} "
                "after the restore, expected active"
            )
        if failures:
            raise Abort
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
        choices=["resets-duty", "fresh-hours"],
        help="doctor the leg's expectations — the pass must fail "
        "naming the carried evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = rotation_pass(args, args.tamper)
    except Abort as abort:
        for line in abort.args:
            eprint(f"rotation: {line}")
        return 1
    for failure in failures:
        eprint(f"rotation: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"rotation: the {args.tamper} case passed silently — "
                "the leg never noticed the doctored expectation"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"rotation-digest {digest} — tracking by tick "
        f"{evidence['converged']}, first cycle at tick "
        f"{evidence['first_cycle_at']}, second at tick "
        f"{evidence['second_cycle_at']}, promoted at tick "
        f"{evidence['promoted_at']}, third at tick "
        f"{evidence['third_cycle_at']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
