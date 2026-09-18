#!/usr/bin/env python3
"""The per-pump manual-takeover leg for the reference plant — the
consumer-side proof that the emitted model's declared per-pump mode
contract holds on the deployed redundant pair (WW-ENG-003,
WW-OPS-001, WW-CTL-002).

The pair leg (`ci/pair.py`) proves the manifest-declared pair runs and
switches; the refusal leg proves its role-gated refusals. This leg
exercises the operator seam those deployments exist for: a customer's
operator taking one pump to manual through the receipted command path
while the pair stands settled and the pump group holds a duty demand.
The rig reads the standby wiring and persistence fields out of
`deploy/manifest.json` and spawns the released tooling exactly as the
pair legs do. The run:

- converges the declared standby to `tracking` through the pair leg's
  driven-tick loop, then drives the simulated plant until the pump
  group holds a duty demand on pump 1 — the group's `cmd_1` request
  delivered to the running machine;
- submits a receipted `write_value` on `p101-mode` through
  `POST /command` on the active's monitor, asserting the `accepted`
  submission, the `applied` receipt settling identically into both
  peers' adopted log, the pump's delivered command leaving the group's
  `cmd_1` — the auto-leg carriers reporting the manual selection —
  and the pump-group status reflecting the exclusion: the pump's
  aggregated availability drops and the group hands the standing
  demand to pump 2;
- writes `p101-hand`, asserting the pump runs on the operator demand
  while the declared thermal/moisture guards still gate it — the
  health carriers standing inside the availability aggregation that
  keeps the pump out of the group's roster — then drives the
  protection layer's reported run state through the plant protocol's
  unfenced `inject_fault` surface, asserting the defeat: the motor's
  proven command/feedback `fault` asserting beside its managed alarm's
  standing and unacknowledged flags, each transition journaled; the
  fault then clears through `clear_fault` and the latch releases
  through a receipted `ack` write;
- writes `p101-oos`, asserting the maintenance inhibit: the in-service
  guard cuts the hand request off the motor's command path, and the
  fault alarm's managed `out_of_service`/`suppressed` states
  annunciate the deliberately offline machine;
- restores `mode`/`oos`/`hand`, asserting the pump rejoins the group's
  roster — the auto and availability carriers standing again — and its
  delivered command follows the group's request once more;
- audits the record: the active's served `GET /journal` must carry
  each receipted write's `applied` settlement attributed to the leg's
  actor beside the journaled point transitions, in the run's `seq`
  order — the pair's one attributed audit trail for the takeover.

Usage:

    takeover.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `takeover-digest <sha256>` line prints — the check runs
two passes and compares them (`takeover-nondeterministic`). A contract
violation reports `takeover: …` lines on stderr and exits 1 — the
check's `takeover-failed`. `--tamper follows-group` doctors the leg's
own expectation — asserting the delivered command still follows the
group while `mode` stands manual — so the leg proves its manual-leg
assertion fires rather than passing an unexercised contract.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

import pair
import simulate


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The driven ticks each phase runs — the pair leg's convergence count,
# the bound on the duty-demand wait, and the bound each phase's
# declared effect gets to land across the wiring's one-scan carrier
# crossings. The actor the leg's receipted submissions declare.
CONVERGE_TICKS = pair.CONVERGE_TICKS
DEMAND_BOUND = 24
SETTLE_BOUND = 16
ACTOR = "ci-takeover"

# The signal names resolving the leg's point ids out of the emitted
# model — the same names the signal index serves, so the leg exercises
# the declared seam, never a hard-coded id.
SIGNALS = {
    "demand": "demand",
    "duty": "duty",
    "staged": "staged",
    "mode": "p101-mode",
    "hand": "p101-hand",
    "oos": "p101-oos",
    "group_cmd": "p101-group-cmd",
    "auto": "p101-auto",
    "oos_ok": "p101-oos-ok",
    "fault": "p101-fault",
    "thermal_ok": "p101-thermal-ok",
    "moisture_ok": "p101-moisture-ok",
    "avail": "p101-avail",
    "cmd": "p101-cmd",
    "run": "p101-run",
    "fault_ack": "p101-fault-ack",
    "fault_alarm": "p101-fault-alarm",
    "fault_unack": "p101-fault-unacknowledged",
    "fault_suppressed": "p101-fault-suppressed",
    "fault_oos": "p101-fault-out-of-service",
    "other_cmd": "p102-cmd",
}


def signal_points(model):
    """The `{name: point id}` map the emitted model's signal index
    declares for the leg's per-pump mode seam — None when the model
    declares no such seam. A signal's `source` is the point it names;
    the lowest-signal-id-wins rule the served index applies."""
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
    for key in ("mode", "hand", "oos", "fault_ack"):
        if points[key] not in writable:
            return None
    return points


def value(snapshot, point):
    """The point's latest sample value — `simulate.snapshot_point`."""
    return simulate.snapshot_point(snapshot, point)


def write_value(point, value):
    """A `write_value` command body for the receipted path."""
    return {
        "write_value": {
            "kind": "bool",
            "point": point,
            "value": {"bool": value},
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


def tick(standby_url, duty_url, failures):
    """One driven pair tick — the tracking peer scanned first so its
    pull applies the owner's latest checkpoint, then the field owner —
    asserting the peers' images stay identical. Returns the owner's
    served snapshot."""
    tracked = pair.scan(standby_url, failures)
    owner = pair.scan(duty_url, failures)
    if pair.select_snapshot(tracked) != pair.select_snapshot(owner):
        failures.append(
            "the tracking peer's image diverged from the field "
            f"owner's at tick {owner['tick']}"
        )
        raise Abort
    return owner


def run_ticks(standby_url, duty_url, failures, count):
    """`count` driven pair ticks; returns the last owner snapshot."""
    owner = None
    for _ in range(count):
        owner = tick(standby_url, duty_url, failures)
    return owner


def drive_until(standby_url, duty_url, failures, condition):
    """Driven pair ticks until `condition(owner_snapshot)` holds —
    returns the satisfying snapshot, or None when SETTLE_BOUND scans
    pass without it landing. The carried-point seam crosses one hop
    per scan, so each receipted write's declared effect takes a few
    scans to arrive; the bound names a landing that never did."""
    for _ in range(SETTLE_BOUND):
        owner = tick(standby_url, duty_url, failures)
        if condition(owner):
            return owner
    return None


def journal_events(journal):
    """The leg's audit stream out of a served `GET /journal` entry
    list — `("settled", point, value, outcome, actor)` for each
    command receipt, `("changed", point, to)` for each journaled value
    transition, and `("quality", point, to)` for each quality
    transition — in served `seq` order."""
    events = []
    for entry in journal:
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
        elif "quality_changed" in event:
            change = event["quality_changed"]
            events.append(("quality", change.get("point"), change.get("to")))
    return events


def ordered_group_misses(journal, groups):
    """The ordered-audit check: each group's events must appear in the
    journal's `seq` order after the previous group's — the leg's
    attributed transitions landing in run order. Returns the named
    misses."""
    events = journal_events(journal)
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
                    f"the journal carries no {want} at or after "
                    f"group {index}'s position — the attributed "
                    "transition is missing or out of order"
                )
            else:
                positions.append(position)
        if positions:
            cursor = max(positions) + 1
    return failures


def takeover_pass(args, tamper):
    """The takeover run: converge, demand, mode, hand and protection,
    out of service, restore, audit. Returns `(digest_entries,
    evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the takeover leg "
            "has nothing to exercise"
        )
    manifest, duty_decl, standby_decl = declared
    with open(args.model) as handle:
        model = json.load(handle)
    points = signal_points(model)
    if points is None:
        raise Abort(
            "the emitted model declares no writable per-pump mode seam "
            "— the takeover leg has nothing to exercise"
        )
    scratch = tempfile.mkdtemp(prefix="dcs-takeover-")
    digest_entries, evidence, failures = [], {}, []
    plant = duty = standby = None
    try:

        def persistence(entry):
            """The controller's declared persistence file basenames
            instantiated under the leg's runner-owned scratch directory
            — the same manifest fields the pair leg honors."""
            root = os.path.join(scratch, entry["name"])
            os.makedirs(root, exist_ok=True)
            return {
                field: os.path.join(root, os.path.basename(entry[field]))
                if entry.get(field)
                else None
                for field in ("state_file", "journal_file")
            }

        duty_files = persistence(duty_decl)
        standby_files = persistence(standby_decl)

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
        plant_addr = simulate.listen_address(plant, "dcs-plant-server")
        plant_io = simulate.PlantClient(plant_addr)

        duty, duty_url, preamble = pair.spawn_peer(
            args.controller, args.model, args.dt, plant_addr, None, duty_files
        )
        if duty_url is None:
            raise Abort(
                f"the duty controller {duty_decl['name']} exited at "
                f"startup: {'; '.join(preamble) or 'no diagnostic'}"
            )
        standby, standby_url, preamble = pair.spawn_peer(
            args.controller,
            args.model,
            args.dt,
            plant_addr,
            duty_url.removeprefix("http://"),
            standby_files,
        )
        if standby_url is None:
            raise Abort(
                f"the standby controller {standby_decl['name']} exited at "
                f"startup: {'; '.join(preamble) or 'no diagnostic'}"
            )

        # Phase 1 — convergence: the pair leg's driven-tick loop, the
        # tracking peer scanned first so each pull applies the owner's
        # latest checkpoint and the peers rest identical.
        ticks = []
        for _ in range(CONVERGE_TICKS):
            owner = tick(standby_url, duty_url, failures)
            ticks.append(owner["tick"])
        standby_role = pair.get(f"{standby_url}/role", "GET /role", failures)
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        sync = standby_role.get("sync")
        if standby_role.get("role") != "standby" or not (
            isinstance(sync, dict) and "tracking" in sync
        ):
            failures.append(
                "the tracking peer never reported tracking — GET /role "
                f"answers {standby_role}"
            )
            raise Abort
        if duty_role.get("role") != "active":
            failures.append(
                f"the field owner reports {duty_role.get('role')!r}, "
                "expected active"
            )
            raise Abort
        evidence["converged"] = ticks[-1]
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": ticks,
                "duty_role": duty_role,
                "standby_role": standby_role,
            }
        )

        # Phase 2 — the duty demand: the simulated well fills under the
        # declared inflow until the pump group holds a duty demand on
        # pump 1 — the group's `cmd_1` request delivered to the running
        # machine, its availability reported into the roster.
        demand_at = None
        for _ in range(DEMAND_BOUND):
            owner = tick(standby_url, duty_url, failures)
            if (
                value(owner, points["demand"]).get("int", 0) >= 1
                and value(owner, points["group_cmd"]) == {"bool": True}
                and value(owner, points["cmd"]) == {"bool": True}
                and value(owner, points["run"]) == {"bool": True}
                and value(owner, points["avail"]) == {"bool": True}
            ):
                demand_at = owner["tick"]
                break
        if demand_at is None:
            failures.append(
                "the pump group never held a duty demand on pump 1 — "
                "the takeover leg's precondition never arrived"
            )
            raise Abort
        if value(owner, points["duty"]) != {"int": 1}:
            failures.append(
                f"the group's duty designation reads "
                f"{value(owner, points['duty'])}, expected pump 1 — "
                "the takeover leg's precondition"
            )
            raise Abort
        evidence["demand_at"] = demand_at
        digest_entries.append(
            {
                "phase": "demand",
                "tick": demand_at,
                "demand": value(owner, points["demand"]),
                "staged": value(owner, points["staged"]),
            }
        )

        # Phase 3 — the receipted takeover: `p101-mode` through
        # `POST /command`'s bounded path. The write must answer
        # `accepted`, settle `applied` identically into both peers'
        # adopted log, and land the pump's delivered command off the
        # group's `cmd_1`: the auto-leg carriers report the manual
        # selection while the group request still stands.
        mode_write = write_value(points["mode"], True)
        receipt = submit(duty_url, mode_write, failures)
        owner = drive_until(
            standby_url,
            duty_url,
            failures,
            lambda snapshot: value(snapshot, points["mode"]) == {"bool": True}
            and value(snapshot, points["auto"]) == {"bool": False}
            and value(snapshot, points["cmd"]) == {"bool": False}
            and value(snapshot, points["avail"]) == {"bool": False},
        )
        if owner is None:
            owner = tick(standby_url, duty_url, failures)
            failures.append(
                "the receipted mode write's effects never landed — "
                f"mode reads {value(owner, points['mode'])}, the auto "
                f"leg {value(owner, points['auto'])}, the delivered "
                f"command {value(owner, points['cmd'])} against the "
                f"group request {value(owner, points['group_cmd'])}, "
                f"availability {value(owner, points['avail'])}"
            )
            raise Abort
        if tamper == "follows-group":
            # The doctored expectation: the leg asserts the delivered
            # command still follows the group's request while `mode`
            # stands manual — the honest manual selection must fail it,
            # naming the auto leg's actual reading.
            if not (
                value(owner, points["auto"]) == {"bool": True}
                and value(owner, points["cmd"])
                == value(owner, points["group_cmd"])
            ):
                failures.append(
                    "the pump did not follow the group while mode "
                    f"stood manual — the auto leg reads "
                    f"{value(owner, points['auto'])} and the delivered "
                    f"command {value(owner, points['cmd'])} against "
                    f"the group request "
                    f"{value(owner, points['group_cmd'])}"
                )
                raise Abort
        receipts_duty = pair.get(f"{duty_url}/receipts", "GET /receipts", failures)
        receipts_standby = pair.get(
            f"{standby_url}/receipts", "GET /receipts", failures
        )
        if receipts_duty != receipts_standby:
            failures.append(
                "the peers' receipt logs diverged — the adopted audit "
                "is not one log"
            )
            raise Abort
        if not settled(receipts_duty, mode_write):
            failures.append(
                "the mode write never settled applied into the adopted "
                "receipt log"
            )
            raise Abort
        # The exclusion lands: the group hands the standing demand to
        # pump 2 — duty designation moves and pump 2's command stands.
        owner = drive_until(
            standby_url,
            duty_url,
            failures,
            lambda snapshot: value(snapshot, points["duty"]) == {"int": 2}
            and value(snapshot, points["other_cmd"]) == {"bool": True},
        )
        if owner is None:
            owner = tick(standby_url, duty_url, failures)
            failures.append(
                f"the pump-group status does not reflect the exclusion "
                f"— duty reads {value(owner, points['duty'])} and pump "
                f"2's command {value(owner, points['other_cmd'])}, "
                "expected duty 2 with pump 2 commanded"
            )
            raise Abort
        exclusion = {
            "duty": value(owner, points["duty"]),
            "other_cmd": value(owner, points["other_cmd"]),
            "avail": value(owner, points["avail"]),
            "group_cmd": value(owner, points["group_cmd"]),
            "cmd": value(owner, points["cmd"]),
        }
        evidence["excluded_at"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "mode",
                "receipt": receipt,
                "exclusion": exclusion,
            }
        )

        # Phase 4 — the operator demand and the protection layer:
        # `p101-hand` runs the pump while the declared thermal/moisture
        # guards still gate it — the health carriers standing inside
        # the availability aggregation that keeps the pump out of the
        # group's roster.
        hand_write = write_value(points["hand"], True)
        receipt = submit(duty_url, hand_write, failures)
        owner = drive_until(
            standby_url,
            duty_url,
            failures,
            lambda snapshot: value(snapshot, points["cmd"]) == {"bool": True}
            and value(snapshot, points["run"]) == {"bool": True},
        )
        if owner is None:
            failures.append(
                "the receipted hand write never ran the pump — the "
                "delivered command or the run feedback never asserted "
                "on the operator demand"
            )
            raise Abort
        for point_name, want in (
            ("thermal_ok", {"bool": True}),
            ("moisture_ok", {"bool": True}),
            ("avail", {"bool": False}),
        ):
            got = value(owner, points[point_name])
            if got != want:
                failures.append(
                    f"the operator demand's declared guards do not "
                    f"stand — point {points[point_name]} ({point_name}) "
                    f"reads {got}, expected {want}"
                )
        if failures:
            raise Abort
        if not settled(
            pair.get(f"{duty_url}/receipts", "GET /receipts", failures),
            hand_write,
        ):
            failures.append(
                "the hand write never settled applied into the adopted "
                "receipt log"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "hand",
                "receipt": receipt,
                "running": {
                    "cmd": value(owner, points["cmd"]),
                    "run": value(owner, points["run"]),
                },
            }
        )

        # The protection input through the plant protocol: an injected
        # non-Good on the run contact defeats the feedback's proof —
        # the motor's `fault` asserts and its managed alarm
        # annunciates; the defeat clears through `clear_fault` and the
        # latch releases through a receipted `ack`.
        verdict = plant_io.request(
            {
                "op": "inject_fault",
                "point": points["run"],
                "fault": {"quality": {"bad": "device_fault"}},
            }
        )
        if verdict.get("result") != "done":
            failures.append(
                f"inject_fault on the run contact answered {verdict}"
            )
            raise Abort
        owner = drive_until(
            standby_url,
            duty_url,
            failures,
            lambda snapshot: value(snapshot, points["fault"]) == {"bool": True}
            and value(snapshot, points["fault_alarm"]) == {"bool": True}
            and value(snapshot, points["fault_unack"]) == {"bool": True},
        )
        if owner is None:
            owner = tick(standby_url, duty_url, failures)
            failures.append(
                f"the protection input's defeat never surfaced — "
                f"fault reads {value(owner, points['fault'])}, alarm "
                f"{value(owner, points['fault_alarm'])}, unacknowledged "
                f"{value(owner, points['fault_unack'])}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "defeat",
                "injected": points["run"],
                "fault": value(owner, points["fault"]),
                "alarm": value(owner, points["fault_alarm"]),
                "unacknowledged": value(owner, points["fault_unack"]),
            }
        )
        verdict = plant_io.request({"op": "clear_fault", "point": points["run"]})
        if verdict.get("result") != "done":
            failures.append(f"clear_fault on the run contact answered {verdict}")
            raise Abort
        owner = drive_until(
            standby_url,
            duty_url,
            failures,
            lambda snapshot: value(snapshot, points["fault"]) == {"bool": False}
            and value(snapshot, points["fault_alarm"]) == {"bool": False},
        )
        if owner is None:
            owner = tick(standby_url, duty_url, failures)
            failures.append(
                f"the cleared protection input left the fault standing "
                f"— fault reads {value(owner, points['fault'])}, alarm "
                f"{value(owner, points['fault_alarm'])}"
            )
            raise Abort
        if value(owner, points["fault_unack"]) != {"bool": True}:
            failures.append(
                f"the cleared fault's latch released without the "
                f"operator's ack — unacknowledged reads "
                f"{value(owner, points['fault_unack'])}"
            )
            raise Abort
        ack_write = write_value(points["fault_ack"], True)
        receipt = submit(duty_url, ack_write, failures)
        owner = drive_until(
            standby_url,
            duty_url,
            failures,
            lambda snapshot: value(snapshot, points["fault_unack"])
            == {"bool": False},
        )
        if owner is None:
            failures.append(
                f"the receipted ack never cleared the latch — "
                f"unacknowledged reads "
                f"{value(owner, points['fault_unack'])}"
            )
            raise Abort
        ack_release = write_value(points["fault_ack"], False)
        submit(duty_url, ack_release, failures)
        owner = run_ticks(standby_url, duty_url, failures, 1)
        digest_entries.append(
            {
                "phase": "cleared",
                "ack_receipt": receipt,
                "unacknowledged": value(owner, points["fault_unack"]),
            }
        )

        # Phase 5 — the maintenance inhibit: `p101-oos` takes the pump
        # out of service. The in-service guard cuts the hand request
        # off the motor's command path, and the fault alarm's managed
        # `out_of_service`/`suppressed` states annunciate the
        # deliberately offline machine.
        oos_write = write_value(points["oos"], True)
        receipt = submit(duty_url, oos_write, failures)
        owner = drive_until(
            standby_url,
            duty_url,
            failures,
            lambda snapshot: value(snapshot, points["cmd"]) == {"bool": False}
            and value(snapshot, points["run"]) == {"bool": False}
            and value(snapshot, points["fault_oos"]) == {"bool": True}
            and value(snapshot, points["fault_suppressed"]) == {"bool": True},
        )
        if owner is None:
            owner = tick(standby_url, duty_url, failures)
            failures.append(
                f"the maintenance inhibit did not hold — cmd reads "
                f"{value(owner, points['cmd'])}, run "
                f"{value(owner, points['run'])}, out_of_service "
                f"{value(owner, points['fault_oos'])}, suppressed "
                f"{value(owner, points['fault_suppressed'])}"
            )
            raise Abort
        if value(owner, points["avail"]) != {"bool": False}:
            failures.append(
                f"the out-of-service pump rejoined the group's roster "
                f"— availability reads {value(owner, points['avail'])}"
            )
            raise Abort
        if not settled(
            pair.get(f"{duty_url}/receipts", "GET /receipts", failures),
            oos_write,
        ):
            failures.append(
                "the out-of-service write never settled applied into "
                "the adopted receipt log"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "oos",
                "receipt": receipt,
                "inhibited": {
                    "cmd": value(owner, points["cmd"]),
                    "out_of_service": value(owner, points["fault_oos"]),
                    "suppressed": value(owner, points["fault_suppressed"]),
                },
            }
        )

        # Phase 6 — the restore: `mode`/`oos`/`hand` return the pump to
        # group control. The auto and in-service carriers stand again,
        # the aggregated availability rejoins the roster, and the
        # delivered command follows the group's request once more.
        restored = []
        for key in ("mode", "oos", "hand"):
            command = write_value(points[key], False)
            submit(duty_url, command, failures)
            restored.append(command)
        owner = drive_until(
            standby_url,
            duty_url,
            failures,
            lambda snapshot: value(snapshot, points["auto"]) == {"bool": True}
            and value(snapshot, points["oos_ok"]) == {"bool": True}
            and value(snapshot, points["avail"]) == {"bool": True}
            and value(snapshot, points["fault_oos"]) == {"bool": False}
            and value(snapshot, points["fault_suppressed"]) == {"bool": False}
            and value(snapshot, points["cmd"])
            == value(snapshot, points["group_cmd"]),
        )
        if owner is None:
            owner = tick(standby_url, duty_url, failures)
            failures.append(
                f"the restore did not return the pump to group "
                f"control — auto reads {value(owner, points['auto'])}, "
                f"in_service {value(owner, points['oos_ok'])}, "
                f"availability {value(owner, points['avail'])}, the "
                f"delivered command {value(owner, points['cmd'])} "
                f"against the group request "
                f"{value(owner, points['group_cmd'])}"
            )
            raise Abort
        receipts_duty = pair.get(f"{duty_url}/receipts", "GET /receipts", failures)
        for command in restored:
            if not settled(receipts_duty, command):
                failures.append(
                    f"the restore write {command} never settled "
                    "applied into the adopted receipt log"
                )
        if failures:
            raise Abort
        evidence["restored_at"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "restore",
                "auto": value(owner, points["auto"]),
                "avail": value(owner, points["avail"]),
                "cmd": value(owner, points["cmd"]),
                "group_cmd": value(owner, points["group_cmd"]),
            }
        )

        # Phase 7 — the audit: the active's served journal carries each
        # attributed transition in run order — every receipted write's
        # applied settlement named to the leg's actor, beside the
        # journaled mode, protection, alarm, inhibit, and restore
        # transitions.
        journal = pair.get(f"{duty_url}/journal", "GET /journal", failures)
        groups = [
            # The takeover: the receipted mode write and the manual
            # selection's journaled transition.
            [
                ("settled", points["mode"], {"bool": True}, "applied", ACTOR),
                ("changed", points["mode"], {"bool": True}),
            ],
            # The operator demand: the hand write's settlement.
            [
                ("settled", points["hand"], {"bool": True}, "applied", ACTOR),
                ("changed", points["run"], {"bool": True}),
            ],
            # The protection input's defeat: the injected quality, the
            # proven fault, and the managed alarm's annunciation.
            [
                ("quality", points["run"], {"bad": "device_fault"}),
                ("changed", points["fault"], {"bool": True}),
                ("changed", points["fault_alarm"], {"bool": True}),
                ("changed", points["fault_unack"], {"bool": True}),
            ],
            # The clear and the receipted ack releasing the latch.
            [
                ("quality", points["run"], "good"),
                ("changed", points["fault"], {"bool": False}),
                ("changed", points["fault_alarm"], {"bool": False}),
            ],
            [
                ("settled", points["fault_ack"], {"bool": True}, "applied", ACTOR),
                ("changed", points["fault_unack"], {"bool": False}),
                ("settled", points["fault_ack"], {"bool": False}, "applied", ACTOR),
            ],
            # The maintenance inhibit: the out-of-service write and the
            # managed states' annunciation.
            [
                ("settled", points["oos"], {"bool": True}, "applied", ACTOR),
                ("changed", points["oos"], {"bool": True}),
                ("changed", points["fault_oos"], {"bool": True}),
                ("changed", points["fault_suppressed"], {"bool": True}),
            ],
            # The restore: each write's settlement and the returned
            # carriers — the pump back in the group's roster.
            [
                ("settled", points["mode"], {"bool": False}, "applied", ACTOR),
                ("settled", points["oos"], {"bool": False}, "applied", ACTOR),
                ("settled", points["hand"], {"bool": False}, "applied", ACTOR),
                ("changed", points["mode"], {"bool": False}),
                ("changed", points["oos"], {"bool": False}),
                ("changed", points["fault_oos"], {"bool": False}),
                ("changed", points["fault_suppressed"], {"bool": False}),
                ("changed", points["avail"], {"bool": True}),
            ],
        ]
        failures.extend(ordered_group_misses(journal, groups))
        if failures:
            raise Abort
        digest_entries.append(
            {"phase": "audit", "events": journal_events(journal)}
        )
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        pair.stop(standby)
        pair.stop(duty)
        pair.stop(plant)
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
        choices=["follows-group"],
        help="doctor the leg's own expectation — the pass must fail "
        "naming the auto leg's actual reading",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = takeover_pass(args, args.tamper)
    except Abort as abort:
        for line in abort.args:
            eprint(f"takeover: {line}")
        return 1
    for failure in failures:
        eprint(f"takeover: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"takeover: the {args.tamper} case passed silently — "
                "the leg never noticed the pump leaving the group"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"takeover-digest {digest} — tracking by tick "
        f"{evidence['converged']}, duty demand at tick "
        f"{evidence['demand_at']}, excluded at tick "
        f"{evidence['excluded_at']}, restored at tick "
        f"{evidence['restored_at']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
