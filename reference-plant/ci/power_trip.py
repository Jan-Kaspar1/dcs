#!/usr/bin/env python3
"""The pair contract's power-fail interlock leg — the station
power-fail trip and its declared recovery proven on the deployed
consumer pair (WW-ENG-003, WW-OPS-001, WW-CTL-002).

The consumer model wires the protection-layer contact into
availability exactly as the platform's own fixtures do —
`power-fail` → `power-ok` → each pump's `power-ok-in` feeding its
`avail_i` aggregate — and carries the managed `power-fail-*` alarm
set; the sibling legs drive the contact only as one consequential
cascade member (the burst leg) and never prove the interlock's
demand behavior or its declared recovery. This leg does. The rig
reads the standby wiring and persistence fields out of
`deploy/manifest.json` and spawns the released tooling exactly as
the pair legs do. The run:

- converges the declared standby to `tracking` through the pair
  leg's driven-tick loop, then drives the simulated plant until the
  pump group holds a full demand at a provably high level — both
  pumps staged, their field commands standing, and the `high-level`
  condition up — the healthy precondition the trip interrupts;
- drives the `power-fail` contact through the plant protocol — the
  field-side `write` the released server fences to the field owner's
  writer claim where the release records one (this leg attaches
  under the active's reported owner token; the earlier release line
  leaves the field unclaimed until a promotion, so the same write
  simply lands) — and asserts the field outputs still read energized
  before the next driven scan: the interlock's release lands on the
  deterministic scan sequence, never on an out-of-band step;
- asserts through the active's monitor that `power-ok` drops, both
  pumps' availability reports unavailable — the aggregate losing its
  power leg — the motor commands release while the chain's `demand`
  still stands, `none-available` annunciates, and the managed
  `power-fail` alarm's `alarm`/`unacknowledged` assert, the durable
  journal carrying the `point_changed` evidence;
- submits `power-fail-ack` through the receipted path — the
  `accepted` submission settling `applied` under the leg's actor —
  asserting the latch clears while the driven condition still
  stands: the contact held, the alarm reporting process truth;
- releases `power-fail` and asserts the declared recovery: the field
  outputs still read released before the first post-restore scan —
  no output step outside the driven sequence — `power-ok` and both
  pumps' availability return, the alarms return while the
  acknowledged latch stays down and the never-acknowledged
  `none-available` latch holds, and the group re-stages the standing
  demand inside the declared bounds — no motor command re-asserting
  inside its `min_off_ticks` holdout, the lag's start inside the
  declared `start_delay_ticks` of the duty's, each `cmd`/`run`
  field pair proving the delivered start;
- audits the record: the pair's controller roles never move — a
  field contact is a plant event, not a failover — and the field
  owner's durable journal file carries the driven transitions and
  the attributed settlements in `seq` order, the served `GET
  /journal` answering the same record.

Usage:

    power_trip.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `power-trip-digest <sha256>` line prints — the check
runs two passes and compares them (`power-trip-nondeterministic`). A
contract violation reports `power-trip: …` lines on stderr and exits
1 — the check's `power-trip-failed`. The `--tamper` cases doctor the
leg's own expectations: `commands-standing` requires the motor
commands still standing after the driven power-fail, and
`availability-holds` requires the pumps' availability still
reporting — each must fail naming the evidence rather than pass a
power-fail that never tripped the interlock.
"""

import argparse
import hashlib
import json
import os
import re
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "legs")
)

import pair
import simulate


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The bound on the full-demand wait — a settled cold-start pair
# reaches the high-level standing demand well inside it — the bound
# each driven phase's declared effect gets to land across the
# wiring's one-scan carrier crossings, and the bound the refilling
# well's return to full demand gets after the restore. The actor the
# leg's receipted submissions declare.
DEMAND_BOUND = 48
SETTLE_BOUND = 16
RECOVER_BOUND = 48
ACTOR = "ci-power-trip"

# The signal names resolving the leg's point ids out of the emitted
# model — the same names the signal index serves, so the leg
# exercises the declared seam, never a hard-coded id.
SIGNALS = {
    "power_fail": "power-fail",
    "power_ok": "power-ok",
    "power_ok_in_1": "p101-power-ok-in",
    "power_ok_in_2": "p102-power-ok-in",
    "power_ack": "power-fail-ack",
    "power_alarm": "power-fail-alarm",
    "power_unack": "power-fail-unacknowledged",
    "avail_1": "p101-avail",
    "avail_2": "p102-avail",
    "cmd_1": "p101-cmd",
    "cmd_2": "p102-cmd",
    "run_1": "p101-run",
    "run_2": "p102-run",
    "demand": "demand",
    "duty": "duty",
    "staged": "staged",
    "none_available": "none-available",
    "none_alarm": "none-available-alarm",
    "none_unack": "none-available-unacknowledged",
    "level_sel": "level-selected",
    "high_level": "high-level",
}


def signal_points(model):
    """The `{key: point id}` map the emitted model's signal index
    declares for the leg's driven and reported points — None when the
    model declares no such protection seam. A signal's `source` is
    the point it names; the lowest-signal-id-wins rule the served
    index applies. The `power-fail-ack` point must be declared
    writable — the receipted seam the leg commands."""
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
    writable = {
        point["id"] for point in model.get("io_points", []) if point.get("writable")
    }
    if points["power_ack"] not in writable:
        return None
    return points


def group_bounds(model):
    """The declared holdout and staging bounds the emitted model's
    `pump-group` carries — `(min_off_ticks, start_delay_ticks)` —
    resolved from the artifact, never restated. None when the model
    declares no such component."""
    for component in model.get("components", []):
        if component["kind"] == "pump-group":
            parameters = component.get("parameters", {})
            min_off = parameters.get("min_off_ticks", {}).get("int")
            start_delay = parameters.get("start_delay_ticks", {}).get("int")
            if min_off is None or start_delay is None:
                return None
            return min_off, start_delay
    return None


def value(snapshot, point):
    """The point's latest sample value — `simulate.snapshot_point`."""
    return simulate.snapshot_point(snapshot, point)


def flag(snapshot, point):
    """The point's latest Bool sample as a truth value."""
    reading = value(snapshot, point) or {}
    return reading.get("bool") is True


def write_value(point, boolean):
    """A `write_value` command body for the receipted path."""
    return {
        "write_value": {
            "kind": "bool",
            "point": point,
            "value": {"bool": boolean},
        }
    }


def submit(url, command, failures):
    """POST one receipted write to the active's `/command` and assert
    the `accepted` submission — returns the receipt."""
    status, receipt = pair.request(
        f"{url}/command", {"command": command, "actor": ACTOR}
    )
    if status != 200 or simulate.receipt_outcome(receipt) != "accepted":
        failures.append(
            f"the receipted write {command} answered {status} "
            f"{receipt}, expected an accepted receipt"
        )
        raise Abort
    return receipt


def settled(receipts, command):
    """True when the adopted receipt log carries `command` settled
    `applied` — the write's durable outcome."""
    return any(
        entry.get("command") == command
        and simulate.receipt_outcome(entry) == "applied"
        for entry in receipts
    )


def field_write(plant_io, owner_token, point, boolean):
    """One field-side `write` through the plant protocol — the
    unfenced diagnostic surface the pair-stage legs use for driven
    inputs. Where the release records the field owner's writer claim
    (the duty's reported `owner token`), this attachment joins it
    first — `ensure_writer` under the same token — so the write lands
    inside the standing claim; where no claim stands the write lands
    unfenced. Returns the verdict."""
    if owner_token is not None:
        verdict = plant_io.request(
            {"op": "ensure_writer", "owner": owner_token}
        )
        if verdict.get("result") not in ("done", "claimed_shared"):
            return verdict
    return plant_io.request(
        {"op": "write", "point": point, "value": {"bool": boolean}}
    )


def field_read(plant_io, point, failures):
    """The field output's stored value read through the plant
    protocol — the scan-sequence probe: a field output moves only
    when a driven scan writes it, so a read between the leg's plant
    writes and the next scan answers the standing value."""
    response = plant_io.request({"op": "read", "point": point})
    if response.get("result") != "sample":
        failures.append(f"the plant read on point {point} answered {response}")
        raise Abort
    return (response.get("sample") or {}).get("value")


def tick(rig, failures):
    """One driven pair tick — the tracking peer scanned first so its
    pull applies the owner's latest checkpoint, then the field owner —
    asserting the peers' images stay identical. Returns the owner's
    served snapshot."""
    return rig.tick(rig.standby_url, rig.duty_url, failures)[1]


def drive_recording(rig, failures, points, condition, bound=SETTLE_BOUND):
    """Driven pair ticks recording each owner's observed leg points
    until `condition(owner_snapshot)` holds — returns `(owner,
    trace)`, `owner` None when `bound` scans pass without the
    condition landing. The carried-point seam crosses one hop per
    scan, so each driven cause's declared effect takes a few scans to
    arrive; the bound names a landing that never did."""
    trace = []
    for _ in range(bound):
        owner = tick(rig, failures)
        trace.append(
            {
                "tick": owner["tick"],
                **{key: value(owner, point) for key, point in points.items()},
            }
        )
        if condition(owner):
            return owner, trace
    return None, trace


def journal_events(entries):
    """The leg's audit stream out of a journal entry list —
    `("settled", point, value, outcome, actor)` for each command
    receipt and `("changed", point, to)` for each journaled value
    transition — in `seq` order."""
    events = []
    for entry in entries:
        event = entry.get("event", {})
        if "command_settled" in event:
            receipt = event["command_settled"].get("receipt", {})
            write = receipt.get("command", {}).get("write_value", {})
            events.append(
                (
                    "settled",
                    write.get("point"),
                    write.get("value"),
                    simulate.receipt_outcome(receipt),
                    receipt.get("actor"),
                )
            )
        elif "point_changed" in event:
            change = event["point_changed"]
            events.append(("changed", change.get("point"), change.get("to")))
    return events


def ordered_group_misses(events, groups):
    """The ordered-audit check: each group's events must appear in
    the record's `seq` order after the previous group's — the leg's
    driven transitions and attributed settlements landing in run
    order. Returns the named misses."""
    failures = []
    cursor = 0
    for index, group in enumerate(groups):
        positions = []
        for want in group:
            position = next(
                (at for at in range(cursor, len(events)) if events[at] == want),
                None,
            )
            if position is None:
                failures.append(
                    f"the durable journal carries no {want} at or "
                    f"after group {index}'s position — the driven "
                    "transition is missing or out of order"
                )
            else:
                positions.append(position)
        if positions:
            cursor = max(positions) + 1
    return failures


def trip_pass(args, tamper):
    """The power-trip run: converge, demand, trip, acknowledge,
    restore, audit. Returns `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the power-trip "
            "leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    with open(args.model) as handle:
        model = json.load(handle)
    points = signal_points(model)
    if points is None:
        raise Abort(
            "the emitted model declares no power-fail protection "
            "seam — the leg has nothing to exercise"
        )
    bounds = group_bounds(model)
    if bounds is None:
        raise Abort(
            "the emitted model declares no pump-group bounds — the "
            "leg's declared min_off/start_delay are unresolvable"
        )
    min_off, start_delay = bounds
    true, false = {"bool": True}, {"bool": False}
    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        duty_files = rig.duty_files
        plant_io = rig.plant_io
        # The field owner's writer claim: a release that claims at
        # startup reports the owner token on stderr ahead of its
        # listener; the earlier release line claims only on promotion,
        # so no token means the field stands unclaimed and the leg's
        # field writes land unfenced.
        owner_token = None
        for line in rig.duty_preamble:
            claimed = re.search(r"owner token (\d+)", line)
            if claimed:
                owner_token = int(claimed.group(1))

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

        # Phase 2 — the standing demand: the simulated well fills
        # until the pump group holds a full demand at a provably high
        # level — both pumps staged and their field commands standing
        # with the `high-level` condition up — the healthy
        # precondition the trip interrupts. The level matters: the
        # released commands take a few driven scans to reach the
        # field, and the drawing pumps pull the well down meanwhile —
        # tripping while the delivered level still stands above the
        # declared `high` setpoint is what keeps the chain's demand
        # standing through the permissive collapse. The run contacts
        # are not in the gate: the sim's loopback proves them through
        # the trip window itself.
        demand_at = None
        for _ in range(DEMAND_BOUND):
            owner = tick(rig, failures)
            if (
                (value(owner, points["demand"]) or {}).get("int", 0) >= 2
                and (value(owner, points["staged"]) or {}).get("int", 0) >= 2
                and flag(owner, points["cmd_1"])
                and flag(owner, points["cmd_2"])
                and flag(owner, points["avail_1"])
                and flag(owner, points["avail_2"])
                and flag(owner, points["high_level"])
            ):
                demand_at = owner["tick"]
                break
        if demand_at is None:
            failures.append(
                "the pump group never held a full demand — the "
                "power-trip leg's two-pumps-running precondition "
                "never arrived"
            )
            raise Abort
        standing_demand = value(owner, points["demand"])
        standing_duty = value(owner, points["duty"])
        evidence["demand_at"] = demand_at
        digest_entries.append(
            {
                "phase": "demand",
                "tick": demand_at,
                "demand": standing_demand,
                "duty": standing_duty,
                "staged": value(owner, points["staged"]),
            }
        )

        # Phase 3 — the trip: the `power-fail` contact driven through
        # the plant protocol drops `power-ok`, both pumps'
        # availability aggregates lose their power leg, and the group
        # de-stages while the chain's demand still stands — the
        # permissive collapse the managed `power-fail` alarm and the
        # `none-available` roll-up annunciate. The field outputs read
        # energized still when the write lands — the release lands on
        # the driven scan sequence, not on an out-of-band step.
        for key in ("cmd_1", "cmd_2"):
            if field_read(plant_io, points[key], failures) != true:
                failures.append(
                    f"the field's {key} does not read energized "
                    "before the trip — the standing demand's "
                    "delivered commands never reached the field"
                )
                raise Abort
        verdict = field_write(
            plant_io, owner_token, points["power_fail"], True
        )
        if verdict.get("result") != "done":
            failures.append(
                f"the field write on power-fail answered {verdict}"
            )
            raise Abort
        for key in ("cmd_1", "cmd_2"):
            if field_read(plant_io, points[key], failures) != true:
                failures.append(
                    f"the field's {key} stepped off outside the "
                    "driven scan sequence — the interlock's release "
                    "moved without a scan"
                )
        if failures:
            raise Abort
        trip_tick = owner["tick"]
        observed = (
            "power_ok",
            "power_ok_in_1",
            "power_ok_in_2",
            "avail_1",
            "avail_2",
            "cmd_1",
            "cmd_2",
            "run_1",
            "run_2",
            "demand",
            "staged",
            "none_available",
            "none_alarm",
            "none_unack",
            "power_alarm",
            "power_unack",
            "level_sel",
            "high_level",
        )
        owner, trace = drive_recording(
            rig,
            failures,
            {key: points[key] for key in observed},
            lambda snapshot: not flag(snapshot, points["power_ok"])
            and not flag(snapshot, points["avail_1"])
            and not flag(snapshot, points["avail_2"])
            and not flag(snapshot, points["cmd_1"])
            and not flag(snapshot, points["cmd_2"])
            and flag(snapshot, points["none_available"])
            and flag(snapshot, points["power_alarm"])
            and flag(snapshot, points["power_unack"])
            and flag(snapshot, points["none_alarm"])
            and flag(snapshot, points["none_unack"])
            and (value(snapshot, points["staged"]) or {}).get("int", 0)
            == 0,
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                "the power-fail trip never landed its declared "
                f"collapse — power-ok reads "
                f"{value(owner, points['power_ok'])}, availability "
                f"{value(owner, points['avail_1'])} / "
                f"{value(owner, points['avail_2'])}, commands "
                f"{value(owner, points['cmd_1'])} / "
                f"{value(owner, points['cmd_2'])}, none-available "
                f"{value(owner, points['none_available'])}, alarms "
                f"{value(owner, points['power_alarm'])} / "
                f"{value(owner, points['none_alarm'])}"
            )
            raise Abort
        tripped_at = owner["tick"]
        # The doctored expectations: a leg requiring the commands
        # still standing or the availability still reporting under
        # the driven power-fail must name the collapse it saw.
        if tamper == "commands-standing":
            if not flag(owner, points["cmd_1"]) or not flag(
                owner, points["cmd_2"]
            ):
                failures.append(
                    "the motor commands released under the driven "
                    "power-fail — the doctored leg expected the "
                    "commands standing"
                )
        elif tamper == "availability-holds":
            if not flag(owner, points["avail_1"]) or not flag(
                owner, points["avail_2"]
            ):
                failures.append(
                    "the pumps' availability dropped under the "
                    "driven power-fail — the doctored leg expected "
                    "availability still reporting"
                )
        # The chain's demand still stands through the permissive
        # collapse: every trace entry up to and including the one
        # where both pumps' availability first reports unavailable
        # must show the chain still calling — the group de-staged
        # because the interlock dropped its permissives, never
        # because the chain's call was satisfied. Past that entry the
        # still-drawing pumps legitimately pull the level-derived
        # count down — the digest records it.
        for entry in trace:
            if (entry.get("demand") or {}).get("int", 0) < 1:
                failures.append(
                    f"the chain's demand released with the "
                    f"permissives — tick {entry['tick']} reads "
                    f"{entry.get('demand')} against the standing "
                    f"{standing_demand}"
                )
                break
            if (entry.get("avail_1") or {}).get("bool") is not True and (
                entry.get("avail_2") or {}
            ).get("bool") is not True:
                break
        for key in ("power_ok_in_1", "power_ok_in_2"):
            if value(owner, points[key]) != false:
                failures.append(
                    f"the declared power-ok carrier {key} still "
                    f"reads {value(owner, points[key])} — the "
                    "contact's availability leg never dropped"
                )
        release_at = {}
        for key in ("cmd_1", "cmd_2"):
            released = next(
                (
                    entry["tick"]
                    for entry in trace
                    if (entry.get(key) or {}).get("bool") is False
                ),
                None,
            )
            if released is None:
                failures.append(
                    f"{key} never released inside the trip window"
                )
            else:
                release_at[key] = released
        if failures:
            raise Abort
        evidence["tripped_at"] = tripped_at
        digest_entries.append(
            {
                "phase": "trip",
                "driven_at": trip_tick,
                "tripped_at": tripped_at,
                "trace": trace,
            }
        )

        # Phase 4 — the receipted ack: `power-fail-ack` submits
        # `accepted` through the bounded path and settles `applied`
        # under the leg's actor, the `unacknowledged` latch clearing
        # while the driven condition still stands — the contact held,
        # the alarm reporting process truth, the permissives dropped.
        ack_write = write_value(points["power_ack"], True)
        submit(duty_url, ack_write, failures)
        owner = drive_recording(
            rig,
            failures,
            {key: points[key] for key in observed},
            lambda snapshot: not flag(snapshot, points["power_unack"]),
        )[0]
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                "the receipted power-fail ack never cleared the "
                f"latch — unacknowledged reads "
                f"{value(owner, points['power_unack'])}"
            )
            raise Abort
        for key, want, what in (
            ("power_fail", True, "the driven contact dropped"),
            (
                "power_alarm",
                True,
                "the alarm stopped reporting process truth",
            ),
            ("avail_1", False, "p101's availability returned early"),
            ("avail_2", False, "p102's availability returned early"),
        ):
            if flag(owner, points[key]) is not want:
                failures.append(
                    f"the ack did not land while the condition "
                    f"stands — {what}: {key} reads "
                    f"{value(owner, points[key])}"
                )
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
        if not settled(receipts_duty, ack_write):
            failures.append(
                "the power-fail ack never settled applied into the "
                "adopted receipt log"
            )
            raise Abort
        evidence["acknowledged_at"] = owner["tick"]
        ack_release = write_value(points["power_ack"], False)
        submit(duty_url, ack_release, failures)
        owner = tick(rig, failures)
        digest_entries.append(
            {
                "phase": "ack",
                "ack": ack_write,
                "acknowledged_at": evidence["acknowledged_at"],
                "release": ack_release,
                "power_alarm": value(owner, points["power_alarm"]),
                "power_unack": value(owner, points["power_unack"]),
            }
        )

        # Phase 5 — the declared recovery: the contact released, the
        # field outputs still read released ahead of the first
        # post-restore scan, then `power-ok` and both availability
        # legs return and the group re-stages the standing demand
        # inside the declared bounds — no command re-asserting inside
        # its `min_off_ticks` holdout, the lag's start inside the
        # declared `start_delay_ticks` of the duty's — while the
        # acknowledged latch stays down.
        verdict = field_write(
            plant_io, owner_token, points["power_fail"], False
        )
        if verdict.get("result") != "done":
            failures.append(
                f"the restore write on power-fail answered {verdict}"
            )
            raise Abort
        for key in ("cmd_1", "cmd_2"):
            if field_read(plant_io, points[key], failures) != false:
                failures.append(
                    f"the field's {key} re-staged outside the "
                    "driven scan sequence — an output step moved "
                    "without a scan"
                )
        if failures:
            raise Abort
        restore_tick = owner["tick"]
        owner, avail_trace = drive_recording(
            rig,
            failures,
            {key: points[key] for key in observed},
            lambda snapshot: flag(snapshot, points["power_ok"])
            and flag(snapshot, points["avail_1"])
            and flag(snapshot, points["avail_2"])
            and not flag(snapshot, points["power_alarm"])
            and not flag(snapshot, points["none_available"]),
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                "the power-fail release never returned the "
                f"permissives — power-ok reads "
                f"{value(owner, points['power_ok'])}, availability "
                f"{value(owner, points['avail_1'])} / "
                f"{value(owner, points['avail_2'])}, alarms "
                f"{value(owner, points['power_alarm'])} / "
                f"{value(owner, points['none_available'])}"
            )
            raise Abort
        available_at = owner["tick"]
        owner, stage_trace = drive_recording(
            rig,
            failures,
            {key: points[key] for key in observed},
            lambda snapshot: (
                value(snapshot, points["staged"]) or {}
            ).get("int", 0)
            >= 2
            and flag(snapshot, points["cmd_1"])
            and flag(snapshot, points["cmd_2"])
            and flag(snapshot, points["run_1"])
            and flag(snapshot, points["run_2"]),
            bound=RECOVER_BOUND,
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                "the returned availability never re-staged the "
                f"standing demand — staged reads "
                f"{value(owner, points['staged'])}, commands "
                f"{value(owner, points['cmd_1'])} / "
                f"{value(owner, points['cmd_2'])}, runs "
                f"{value(owner, points['run_1'])} / "
                f"{value(owner, points['run_2'])}"
            )
            raise Abort
        # The declared bounds: no motor command re-asserted inside
        # its `min_off_ticks` holdout — measured on the delivered
        # field command, which trails the group's own request — and
        # the second start followed the first inside the declared
        # `start_delay_ticks` on the group's own `staged` report.
        for key in ("cmd_1", "cmd_2"):
            reasserted = next(
                (
                    entry["tick"]
                    for entry in avail_trace + stage_trace
                    if (entry.get(key) or {}).get("bool") is True
                ),
                None,
            )
            if reasserted is None:
                failures.append(f"{key} never re-staged")
            elif reasserted < release_at[key] + min_off:
                failures.append(
                    f"{key} re-staged at tick {reasserted}, inside "
                    f"the declared min_off_ticks holdout — it "
                    f"released at {release_at[key]} and the bound "
                    f"is {min_off}"
                )
        staged_trace = avail_trace + stage_trace
        first_start = next(
            (
                entry
                for entry in staged_trace
                if (entry.get("staged") or {}).get("int", 0) >= 1
            ),
            None,
        )
        full_start = next(
            (
                entry
                for entry in staged_trace
                if (entry.get("staged") or {}).get("int", 0) >= 2
            ),
            None,
        )
        full_call = next(
            (
                entry
                for entry in staged_trace
                if (entry.get("demand") or {}).get("int", 0) >= 2
            ),
            None,
        )
        if first_start is None or full_start is None or full_call is None:
            failures.append(
                "the group never re-staged the demand after the "
                "power-fail release"
            )
        else:
            # The lag's start must land inside the declared
            # `start_delay_ticks` of the second stage-count's arrival
            # — the served `demand` leads the group's delivered
            # `demand-in` by the carrier's one scan, so the bound
            # carries that hop — and the starts must keep the
            # declared minimum spacing between them.
            call_gap = full_start["tick"] - full_call["tick"]
            if call_gap > start_delay + 1 or call_gap < 0:
                failures.append(
                    f"the lag re-start landed {call_gap} ticks "
                    f"after the demand's second stage arrived, "
                    f"outside the declared start_delay_ticks bound "
                    f"{start_delay}"
                )
            start_gap = full_start["tick"] - first_start["tick"]
            if start_gap < start_delay:
                failures.append(
                    f"the re-staged starts landed {start_gap} ticks "
                    f"apart, inside the declared start_delay_ticks "
                    f"minimum spacing {start_delay}"
                )
        for key, want, what in (
            (
                "power_unack",
                False,
                "the acknowledged latch re-asserted without a new "
                "trip",
            ),
            (
                "none_unack",
                True,
                "the never-acknowledged none-available latch "
                "dropped",
            ),
        ):
            if flag(owner, points[key]) is not want:
                failures.append(
                    f"the recovered pair's latches are wrong — "
                    f"{what}: {key} reads "
                    f"{value(owner, points[key])}"
                )
        if failures:
            raise Abort
        evidence["restored_at"] = available_at
        evidence["restaged_at"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "restore",
                "released_at": restore_tick,
                "available_at": available_at,
                "trace": avail_trace + stage_trace,
            }
        )

        # Where the release records the writer claim, this
        # attachment's conditional hold on it releases — the field
        # owner's own hold stands.
        if owner_token is not None:
            verdict = plant_io.request({"op": "release_writer"})
            if verdict.get("result") != "done":
                failures.append(f"release_writer answered {verdict}")
                raise Abort

        # Phase 6 — the record: the pair's roles never moved — a
        # field contact is a plant event, not a failover — and the
        # field owner's durable journal file carries the driven
        # transitions and the attributed settlements in `seq` order,
        # the served journal answering the same record.
        standby_role = pair.get(f"{standby_url}/role", "GET /role", failures)
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        sync = standby_role.get("sync")
        if standby_role.get("role") != "standby" or not (
            isinstance(sync, dict) and "tracking" in sync
        ):
            failures.append(
                "the standby's role moved through the power trip — "
                f"GET /role answers {standby_role}"
            )
        if duty_role.get("role") != "active":
            failures.append(
                "the field owner's role moved through the power "
                f"trip — GET /role answers {duty_role}"
            )
        if failures:
            raise Abort

        journal_path = duty_files.get("journal_file")
        if journal_path is None or not os.path.exists(journal_path):
            failures.append(
                "the field owner's declared journal file does not "
                "exist — the --journal-file flag was not honored"
            )
            raise Abort
        entries = [
            record
            for kind, record in pair.journal_records(journal_path)
            if kind == "entry"
        ]
        seqs = [entry["seq"] for entry in entries]
        if seqs != list(range(1, len(seqs) + 1)):
            failures.append(
                f"the field owner's journal seqs are not 1..n in "
                f"order: {seqs}"
            )
            raise Abort
        served = pair.get(f"{duty_url}/journal", "GET /journal", failures)
        events = journal_events(entries)
        if journal_events(served) != events:
            failures.append(
                "the served journal's transition stream diverges "
                "from the durable file's — the monitor does not "
                "answer the record it persists"
            )
            raise Abort
        for name, journal in (
            (duty_decl["name"], entries),
            (
                standby_decl["name"],
                pair.get(
                    f"{standby_url}/journal", "GET /journal", failures
                ),
            ),
        ):
            transitions = pair.role_transitions(journal)
            if transitions:
                failures.append(
                    f"{name}'s journal carries role changes "
                    f"{transitions} — a field contact must not move "
                    "the pair's roles"
                )

        groups = [
            # The trip: the driven contact, the managed alarm's
            # annunciation, the permissive collapse, and the released
            # commands' proven run releases — in driven order.
            [
                ("changed", points["power_fail"], true),
                ("changed", points["power_alarm"], true),
                ("changed", points["power_unack"], true),
                ("changed", points["avail_1"], false),
                ("changed", points["avail_2"], false),
                ("changed", points["none_available"], true),
                ("changed", points["none_alarm"], true),
                ("changed", points["none_unack"], true),
                ("changed", points["run_1"], false),
                ("changed", points["run_2"], false),
            ],
            # The receipted ack clearing the latch while the
            # condition stands — the attributed settlement beside
            # the journaled latch release.
            [
                ("settled", points["power_ack"], true, "applied", ACTOR),
                ("changed", points["power_unack"], false),
                ("settled", points["power_ack"], false, "applied", ACTOR),
            ],
            # The declared recovery: the restored contact, the
            # returned alarm and permissives, and the re-staged
            # commands' proven runs.
            [
                ("changed", points["power_fail"], false),
                ("changed", points["power_alarm"], false),
                ("changed", points["avail_1"], true),
                ("changed", points["avail_2"], true),
                ("changed", points["none_available"], false),
                ("changed", points["none_alarm"], false),
                ("changed", points["run_1"], true),
                ("changed", points["run_2"], true),
            ],
        ]
        failures.extend(ordered_group_misses(events, groups))
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "audit",
                "duty_role": duty_role,
                "standby_role": standby_role,
                "events": events,
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
        choices=["commands-standing", "availability-holds"],
        help="doctor the leg's own expectations — the pass must fail "
        "naming the trip evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = trip_pass(args, args.tamper)
    except Abort as abort:
        for line in abort.args:
            eprint(f"power-trip: {line}")
        return 1
    for failure in failures:
        eprint(f"power-trip: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"power-trip: the {args.tamper} case passed silently "
                "— the leg never noticed the doctored expectation"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"power-trip-digest {digest} — tracking by tick "
        f"{evidence['converged']}, full demand at tick "
        f"{evidence['demand_at']}, tripped at tick "
        f"{evidence['tripped_at']}, acknowledged at tick "
        f"{evidence['acknowledged_at']}, permissives returned at "
        f"tick {evidence['restored_at']}, re-staged by tick "
        f"{evidence['restaged_at']}, run continued to tick "
        f"{evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
