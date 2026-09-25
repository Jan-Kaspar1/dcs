#!/usr/bin/env python3
"""The consequential-alarm first-out leg for the reference plant — the
consumer-side proof that the deployed redundant pair's durable ordered
transition record keeps activation order through a driven multi-alarm
burst (WW-ENG-003, WW-ALM-003, WW-ALM-004).

The pair leg (`ci/legs/pair.py`) proves the manifest-declared pair runs and
switches; the takeover leg proves the receipted operator seam. This
leg exercises the incident-review record those deployments exist for:
a customer's consequential alarm cascade must land in the field
owner's durable journal in driven order — the first-out ordering the
alarm record exists to give. The rig reads the standby wiring and
persistence fields out of `deploy/manifest.json` and spawns the
released tooling exactly as the pair legs do. The run:

- converges the declared standby to `tracking` through the pair leg's
  driven-tick loop, then drives the simulated plant until the pump
  group holds a duty demand — a pump running and drawing the well
  down, the healthy precondition the burst interrupts;
- injects a non-Good quality on `level-primary` through the plant
  protocol's unfenced fault surface, so the failover's `backup-active`
  annunciates first;
- drives the `power-fail` contact through the plant protocol — the
  field-side `write` the released server fences to the field owner's
  writer claim where the release records one (this leg attaches under
  the active's reported owner token; the earlier release line leaves
  the field unclaimed until a promotion, so the same write simply
  lands) — so the station permissives drop, the pumps de-stage, and
  the power alarm fires while the undrawn level climbs;
- injects a non-Good quality on both pumps' run contacts, so the
  proven command/feedback `fault` asserts on each motor and the
  group's `all_faulted` roll-up lands last — the consequential tail;
- asserts through the active's monitor that every driven alarm's
  `alarm`/`unacknowledged` status stands;
- restores each driven input in the same order — the cleared
  instrument, the restored contact, the healthy run feedback — so the
  journal carries the returns in order beside the assertions;
- audits the record: the active's durable journal file must carry
  every driven activation and return in `seq` order with no dropped
  or reordered entries, the served `GET /journal` answering the same
  record, and the pair's roles unchanged throughout.

Usage:

    burst_order.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `burst-order-digest <sha256>` line prints — the check
runs two passes and compares them (`burst-order-nondeterministic`). A
contract violation reports `burst-order: …` lines on stderr and exits
1 — the check's `burst-order-failed`. `--tamper dropped-transition`
removes one driven activation from the record the audit reads and
`--tamper reordered-transition` swaps two activations' positions — a
journal that dropped or reordered a transition must fail the audit
rather than pass silently.
"""

import argparse
import hashlib
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import pair
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored cases: a record missing a driven transition, or
# one carrying them out of order, must surface the named
# diagnostic — never a silently unexercised first-out proof.
LEG = {
    "order": 110,
    "title": "the alarm-burst leg",
    "passes": "burst-order",
    "tampers": [
        {
            "name": "dropped-transition",
            "passed": "a dropped-transition journal passed the burst leg",
            "missed": "the dropped-transition case did not report its named diagnostic",
            "evidence": ["missing or out of order"],
        },
        {
            "name": "reordered-transition",
            "passed": "a reordered-transition journal passed the burst leg",
            "missed": "the reordered-transition case did not report its named diagnostic",
            "evidence": ["missing or out of order"],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The driven ticks each phase runs — the bound on the duty-demand
# wait, and the bound each phase's declared effect gets to land across
# the wiring's one-scan carrier crossings.
DEMAND_BOUND = 24
SETTLE_BOUND = 16

# The injected non-Good the fault surface carries — the same quality
# the takeover leg's protection drive uses.
BAD_QUALITY = {"bad": "device_fault"}

# The signal names resolving the leg's point ids out of the emitted
# model — the same names the signal index serves, so the leg exercises
# the declared seam, never a hard-coded id.
SIGNALS = {
    "level_primary": "level-primary",
    "level_selected": "level-selected",
    "power_fail": "power-fail",
    "power_ok": "power-ok",
    "demand": "demand",
    "duty": "duty",
    "staged": "staged",
    "backup_active": "backup-active",
    "none_available": "none-available",
    "all_faulted": "all-faulted",
    "run_1": "p101-run",
    "run_2": "p102-run",
    "avail_1": "p101-avail",
    "avail_2": "p102-avail",
    "fault_1": "p101-fault",
    "fault_2": "p102-fault",
    "backup_alarm": "backup-active-alarm",
    "backup_unack": "backup-active-unacknowledged",
    "none_alarm": "none-available-alarm",
    "none_unack": "none-available-unacknowledged",
    "faulted_alarm": "all-faulted-alarm",
    "faulted_unack": "all-faulted-unacknowledged",
    "power_alarm": "power-fail-alarm",
    "power_unack": "power-fail-unacknowledged",
    "fault_alarm_1": "p101-fault-alarm",
    "fault_unack_1": "p101-fault-unacknowledged",
    "fault_alarm_2": "p102-fault-alarm",
    "fault_unack_2": "p102-fault-unacknowledged",
}


def signal_points(model):
    """The `{key: point id}` map the emitted model's signal index
    declares for the leg's driven points — None when the model
    declares no such alarm set. A signal's `source` is the point it
    names; the lowest-signal-id-wins rule the served index applies."""
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


def tick(rig, failures):
    """One driven pair tick — the tracking peer scanned first so its
    pull applies the owner's latest checkpoint, then the field owner —
    asserting the peers' images stay identical. Returns the owner's
    served snapshot."""
    _tracked, owner = rig.tick(rig.standby_url, rig.duty_url, failures)
    return owner


def drive_until(rig, failures, condition):
    """Driven pair ticks until `condition(owner_snapshot)` holds —
    returns the satisfying snapshot, or None when SETTLE_BOUND scans
    pass without it landing. The carried-point seam crosses one hop
    per scan, so each driven cause's declared effect takes a few
    scans to arrive; the bound names a landing that never did."""
    for _ in range(SETTLE_BOUND):
        owner = tick(rig, failures)
        if condition(owner):
            return owner
    return None


def journal_events(entries):
    """The leg's audit stream out of a journal entry list —
    `("changed", point, to)` for each journaled value transition and
    `("quality", point, to)` for each quality transition — in `seq`
    order."""
    events = []
    for entry in entries:
        event = entry.get("event", {})
        if "point_changed" in event:
            change = event["point_changed"]
            events.append(("changed", change.get("point"), change.get("to")))
        elif "quality_changed" in event:
            change = event["quality_changed"]
            events.append(("quality", change.get("point"), change.get("to")))
    return events


def ordered_group_misses(events, groups):
    """The first-out audit: each group's events must appear in the
    record's `seq` order after the previous group's — the driven
    causes' transitions landing in driven order, none dropped and none
    reordered. Returns the named misses."""
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


def field_write(plant_io, owner_token, point, boolean):
    """One field-side `write` through the plant protocol — the unfenced
    diagnostic surface the scenario legs use for driven inputs. Where
    the release records the field owner's writer claim (the duty's
    reported `owner token`), this attachment joins it first —
    `ensure_writer` under the same token — so the write lands inside
    the standing claim; where no claim stands the write lands
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


def burst_pass(args, tamper):
    """The burst run: converge, demand, backup, power, faults, peak,
    restores, audit. Returns `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the burst leg "
            "has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    with open(args.model) as handle:
        model = json.load(handle)
    points = signal_points(model)
    if points is None:
        raise Abort(
            "the emitted model declares no station alarm set — the "
            "burst leg has nothing to exercise"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared, tamper)
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

        # Phase 2 — the duty demand: the simulated well fills until the
        # pump group holds a full demand — both pumps staged, running,
        # and available — the healthy precondition the burst
        # interrupts, with both run contacts proven so the outage's
        # stops journal on each.
        demand_at = None
        for _ in range(DEMAND_BOUND):
            owner = tick(rig, failures)
            if (
                value(owner, points["demand"]).get("int", 0) >= 2
                and value(owner, points["staged"]).get("int", 0) >= 2
                and value(owner, points["run_1"]) == {"bool": True}
                and value(owner, points["run_2"]) == {"bool": True}
                and value(owner, points["avail_1"]) == {"bool": True}
                and value(owner, points["avail_2"]) == {"bool": True}
            ):
                demand_at = owner["tick"]
                break
        if demand_at is None:
            failures.append(
                "the pump group never held a full demand — the burst "
                "leg's two-pumps-running precondition never arrived"
            )
            raise Abort
        evidence["demand_at"] = demand_at
        digest_entries.append(
            {
                "phase": "demand",
                "tick": demand_at,
                "demand": value(owner, points["demand"]),
                "staged": value(owner, points["staged"]),
                "level": value(owner, points["level_selected"]),
            }
        )

        # Phase 3 — the initiating cause: a non-Good quality on
        # `level-primary` fails the measurement over to the backup —
        # `backup-active` annunciates first.
        verdict = plant_io.request(
            {
                "op": "inject_fault",
                "point": points["level_primary"],
                "fault": {"quality": BAD_QUALITY},
            }
        )
        if verdict.get("result") != "done":
            failures.append(
                f"inject_fault on level-primary answered {verdict}"
            )
            raise Abort
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: value(snapshot, points["backup_active"])
            == {"bool": True}
            and value(snapshot, points["backup_alarm"]) == {"bool": True}
            and value(snapshot, points["backup_unack"]) == {"bool": True},
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                "the level-primary fault never annunciated — "
                f"backup-active reads "
                f"{value(owner, points['backup_active'])}, alarm "
                f"{value(owner, points['backup_alarm'])}, "
                f"unacknowledged "
                f"{value(owner, points['backup_unack'])}"
            )
            raise Abort
        evidence["backup_at"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "backup",
                "tick": owner["tick"],
                "alarm": value(owner, points["backup_alarm"]),
                "unacknowledged": value(owner, points["backup_unack"]),
            }
        )

        # Phase 4 — the consequential drive: the `power-fail` contact
        # written through the plant protocol drops the station
        # permissives — the availability aggregation loses its
        # power-ok leg, the group de-stages the pumps, and the undrawn
        # level climbs — while the power alarm fires.
        verdict = field_write(
            plant_io, owner_token, points["power_fail"], True
        )
        if verdict.get("result") != "done":
            failures.append(
                f"the field write on power-fail answered {verdict}"
            )
            raise Abort
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: value(snapshot, points["power_alarm"])
            == {"bool": True}
            and value(snapshot, points["power_unack"]) == {"bool": True}
            and value(snapshot, points["none_alarm"]) == {"bool": True}
            and value(snapshot, points["none_unack"]) == {"bool": True}
            and value(snapshot, points["staged"]).get("int", 0) == 0,
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                "the power-fail drive's consequences never landed — "
                f"power alarm reads "
                f"{value(owner, points['power_alarm'])}, "
                f"unacknowledged "
                f"{value(owner, points['power_unack'])}, "
                f"none-available alarm "
                f"{value(owner, points['none_alarm'])}, "
                f"unacknowledged "
                f"{value(owner, points['none_unack'])}, staged "
                f"{value(owner, points['staged'])}"
            )
            raise Abort
        # The permissives stand dropped: every availability leg reads
        # false and the group holds nothing staged.
        for key in ("power_ok", "avail_1", "avail_2"):
            if value(owner, points[key]) != {"bool": False}:
                failures.append(
                    f"the power-fail permissives did not drop — "
                    f"{key} reads {value(owner, points[key])}, "
                    "expected false"
                )
        if failures:
            raise Abort
        # The level undrawn: with no pump commanded the declared
        # inflow fills the well again — the trough at the de-stage
        # gives way to a climb over the outage's next scans.
        level_low = value(owner, points["level_selected"])
        level_after = level_low
        for _ in range(4):
            owner = tick(rig, failures)
            level_after = value(owner, points["level_selected"])
        if not (
            isinstance(level_low, dict)
            and isinstance(level_after, dict)
            and level_after.get("float", 0) > level_low.get("float", 0)
        ):
            failures.append(
                f"the undrawn level did not climb through the outage "
                f"— {level_low} to {level_after}"
            )
            raise Abort
        evidence["power_at"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "power",
                "tick": owner["tick"],
                "alarm": value(owner, points["power_alarm"]),
                "none_alarm": value(owner, points["none_alarm"]),
                "level_low": level_low,
                "level_after": level_after,
            }
        )

        # Phase 5 — the consequential tail: a non-Good quality on both
        # pumps' run contacts proves the command/feedback `fault` on
        # each motor — the group's `all_faulted` roll-up lands last.
        for key in ("run_1", "run_2"):
            verdict = plant_io.request(
                {
                    "op": "inject_fault",
                    "point": points[key],
                    "fault": {"quality": BAD_QUALITY},
                }
            )
            if verdict.get("result") != "done":
                failures.append(
                    f"inject_fault on {key} answered {verdict}"
                )
                raise Abort
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: value(snapshot, points["faulted_alarm"])
            == {"bool": True}
            and value(snapshot, points["faulted_unack"]) == {"bool": True}
            and value(snapshot, points["fault_alarm_1"]) == {"bool": True}
            and value(snapshot, points["fault_unack_1"]) == {"bool": True}
            and value(snapshot, points["fault_alarm_2"]) == {"bool": True}
            and value(snapshot, points["fault_unack_2"]) == {"bool": True},
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                "the run-contact faults never landed the all-faulted "
                f"tail — all_faulted reads "
                f"{value(owner, points['all_faulted'])}, alarm "
                f"{value(owner, points['faulted_alarm'])}, pump "
                f"faults {value(owner, points['fault_1'])} / "
                f"{value(owner, points['fault_2'])}"
            )
            raise Abort
        evidence["faulted_at"] = owner["tick"]

        # Phase 6 — the peak: through the active's monitor, every
        # driven alarm's `alarm`/`unacknowledged` status stands.
        driven = (
            ("backup", "backup_alarm", "backup_unack"),
            ("power", "power_alarm", "power_unack"),
            ("none-available", "none_alarm", "none_unack"),
            ("all-faulted", "faulted_alarm", "faulted_unack"),
            ("p101-fault", "fault_alarm_1", "fault_unack_1"),
            ("p102-fault", "fault_alarm_2", "fault_unack_2"),
        )
        standing = {}
        for name, alarm_key, unack_key in driven:
            alarm = value(owner, points[alarm_key])
            unack = value(owner, points[unack_key])
            standing[name] = {"alarm": alarm, "unacknowledged": unack}
            if alarm != {"bool": True} or unack != {"bool": True}:
                failures.append(
                    f"the {name} alarm does not stand at the burst's "
                    f"peak — alarm reads {alarm}, unacknowledged "
                    f"{unack}, expected both true"
                )
        if failures:
            raise Abort
        digest_entries.append(
            {"phase": "peak", "tick": owner["tick"], "standing": standing}
        )

        # Phase 7 — the restores, in driven order: the cleared
        # instrument returns `backup-active`, the restored contact
        # returns the power alarm and re-arms the permissives, and the
        # healthy run feedback clears the faults so the group stages
        # again — each return journaled beside its assertion.
        verdict = plant_io.request(
            {"op": "clear_fault", "point": points["level_primary"]}
        )
        if verdict.get("result") != "done":
            failures.append(f"clear_fault on level-primary answered {verdict}")
            raise Abort
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: value(snapshot, points["backup_active"])
            == {"bool": False}
            and value(snapshot, points["backup_alarm"]) == {"bool": False},
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                "the cleared instrument left backup serving — "
                f"backup-active reads "
                f"{value(owner, points['backup_active'])}, alarm "
                f"{value(owner, points['backup_alarm'])}"
            )
            raise Abort
        verdict = field_write(
            plant_io, owner_token, points["power_fail"], False
        )
        if verdict.get("result") != "done":
            failures.append(
                f"the restore write on power-fail answered {verdict}"
            )
            raise Abort
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: value(snapshot, points["power_alarm"])
            == {"bool": False}
            and value(snapshot, points["avail_1"]) == {"bool": True}
            and value(snapshot, points["avail_2"]) == {"bool": True},
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                "the restored contact left the permissives dropped — "
                f"power alarm reads "
                f"{value(owner, points['power_alarm'])}, "
                f"availability "
                f"{value(owner, points['avail_1'])} / "
                f"{value(owner, points['avail_2'])}"
            )
            raise Abort
        for key in ("run_1", "run_2"):
            verdict = plant_io.request(
                {"op": "clear_fault", "point": points[key]}
            )
            if verdict.get("result") != "done":
                failures.append(f"clear_fault on {key} answered {verdict}")
                raise Abort
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: value(snapshot, points["faulted_alarm"])
            == {"bool": False}
            and value(snapshot, points["fault_alarm_1"]) == {"bool": False}
            and value(snapshot, points["fault_alarm_2"]) == {"bool": False}
            and value(snapshot, points["none_alarm"]) == {"bool": False}
            and value(snapshot, points["run_1"]) == {"bool": True}
            and value(snapshot, points["run_2"]) == {"bool": True},
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                "the cleared run contacts left the fault tail "
                f"standing — all_faulted reads "
                f"{value(owner, points['all_faulted'])}, alarms "
                f"{value(owner, points['faulted_alarm'])} / "
                f"{value(owner, points['fault_alarm_1'])} / "
                f"{value(owner, points['fault_alarm_2'])}, staged "
                f"{value(owner, points['staged'])}"
            )
            raise Abort
        evidence["restored_at"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "restore",
                "tick": owner["tick"],
                "staged": value(owner, points["staged"]),
            }
        )

        # Where the release records the writer claim, this attachment's
        # conditional hold on it releases — the field owner's own hold
        # stands.
        if owner_token is not None:
            verdict = plant_io.request({"op": "release_writer"})
            if verdict.get("result") != "done":
                failures.append(f"release_writer answered {verdict}")
                raise Abort

        # Phase 8 — the record: the pair's roles never moved, and the
        # field owner's durable journal file carries every driven
        # activation and return in `seq` order — the first-out record
        # — with no dropped or reordered entries, the served journal
        # answering the same record.
        standby_role = pair.get(f"{standby_url}/role", "GET /role", failures)
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        sync = standby_role.get("sync")
        if standby_role.get("role") != "standby" or not (
            isinstance(sync, dict) and "tracking" in sync
        ):
            failures.append(
                "the standby's role moved through the burst — GET "
                f"/role answers {standby_role}"
            )
        if duty_role.get("role") != "active":
            failures.append(
                f"the field owner's role moved through the burst — "
                f"GET /role answers {duty_role}"
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
                "the served journal's transition stream diverges from "
                "the durable file's — the monitor does not answer the "
                "record it persists"
            )
            raise Abort

        # The doctored record the tampers exercise: a journal that
        # dropped the power alarm's activation, or one that reordered
        # the first two annunciations — the audit must name the
        # violation, never pass it silently.
        if tamper == "dropped-transition":
            events.remove(
                ("changed", points["power_alarm"], {"bool": True})
            )
        elif tamper == "reordered-transition":
            first = events.index(
                ("changed", points["backup_alarm"], {"bool": True})
            )
            second = events.index(
                ("changed", points["power_alarm"], {"bool": True})
            )
            events[first], events[second] = events[second], events[first]

        true = {"bool": True}
        false = {"bool": False}
        groups = [
            # The initiating cause: the instrument's failed quality,
            # the failover's backup-serving flag, and its alarm's
            # annunciation — first.
            [
                ("quality", points["level_primary"], BAD_QUALITY),
                ("changed", points["backup_active"], true),
                ("changed", points["backup_alarm"], true),
                ("changed", points["backup_unack"], true),
            ],
            # The consequential drive: the power-fail contact, its
            # alarm's annunciation, and the permissive collapse — both
            # pumps' availability drops and none-available annunciates.
            # The cause and its collapse land inside the same window:
            # the alarm's condition rides the station power interlock's
            # `tripped` — the quality-aware reading the permissives
            # trip on — so the record may carry either side first.
            [
                ("changed", points["power_fail"], true),
                ("changed", points["power_alarm"], true),
                ("changed", points["power_unack"], true),
                ("changed", points["avail_1"], false),
                ("changed", points["avail_2"], false),
                ("changed", points["none_available"], true),
                ("changed", points["none_alarm"], true),
                ("changed", points["none_unack"], true),
            ],
            # The consequential tail: both run contacts' failed
            # quality, each motor's proven fault, the group's
            # all-faulted roll-up, and the fault alarms — last.
            [
                ("quality", points["run_1"], BAD_QUALITY),
                ("quality", points["run_2"], BAD_QUALITY),
                ("changed", points["fault_1"], true),
                ("changed", points["fault_2"], true),
                ("changed", points["all_faulted"], true),
                ("changed", points["faulted_alarm"], true),
                ("changed", points["faulted_unack"], true),
                ("changed", points["fault_alarm_1"], true),
                ("changed", points["fault_unack_1"], true),
                ("changed", points["fault_alarm_2"], true),
                ("changed", points["fault_unack_2"], true),
            ],
            # The restores in driven order: the instrument's healthy
            # quality returns the backup-serving flag and its alarm.
            [
                ("quality", points["level_primary"], "good"),
                ("changed", points["backup_active"], false),
                ("changed", points["backup_alarm"], false),
            ],
            # The restored contact returns the power alarm and re-arms
            # both pumps' availability.
            [
                ("changed", points["power_fail"], false),
                ("changed", points["power_alarm"], false),
                ("changed", points["avail_1"], true),
                ("changed", points["avail_2"], true),
            ],
            # The healthy run feedback clears each motor's fault, the
            # all-faulted roll-up, and the fault alarms; a pump
            # rejoins — none-available returns — and the run contacts
            # follow the re-staged command.
            [
                ("quality", points["run_1"], "good"),
                ("quality", points["run_2"], "good"),
                ("changed", points["fault_1"], false),
                ("changed", points["fault_2"], false),
                ("changed", points["all_faulted"], false),
                ("changed", points["faulted_alarm"], false),
                ("changed", points["fault_alarm_1"], false),
                ("changed", points["fault_alarm_2"], false),
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
        choices=["dropped-transition", "reordered-transition"],
        help="doctor the record the audit reads — the pass must fail "
        "naming the missing or reordered transition",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = burst_pass(args, args.tamper)
    except Abort as abort:
        for line in abort.args:
            eprint(f"burst-order: {line}")
        return 1
    for failure in failures:
        eprint(f"burst-order: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"burst-order: the {args.tamper} case passed silently "
                "— the leg never noticed the doctored record"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"burst-order-digest {digest} — tracking by tick "
        f"{evidence['converged']}, duty demand at tick "
        f"{evidence['demand_at']}, backup annunciated at tick "
        f"{evidence['backup_at']}, power failed at tick "
        f"{evidence['power_at']}, all faulted at tick "
        f"{evidence['faulted_at']}, restored at tick "
        f"{evidence['restored_at']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
