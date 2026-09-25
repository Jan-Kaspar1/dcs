#!/usr/bin/env python3
"""The threshold-chain staging leg for the reference plant — the
consumer-side proof that the deployed redundant pair stages and
de-stages on level through the emitted model's declared setpoint
chain (WW-ENG-003, WW-CTL-001, WW-CTL-002).

The pair leg (`ci/legs/pair.py`) proves the manifest-declared pair runs and
switches; the takeover leg proves the receipted operator seam. This
leg exercises the process behavior those deployments exist to run:
the emitted consumer model's `threshold-chain` — its declared
`cutoff`/`stop`/`start`/`lag_start`/`high` setpoints — driving the
pump group's `demand` against the declared dynamics, with the
`high` crossing's managed annunciation journaled on the durable
record. The rig reads the standby wiring and persistence fields out
of `deploy/manifest.json` and spawns the released tooling exactly as
the pair legs do. The run:

- submits receipted `write_value` holds on both pumps' declared
  writable `oos` points through the active's `POST /command` before
  the first driven tick — the leg's honest lever against the emitted
  dynamics: with every pump held out of service the declared inflow
  raises the wet-well level unopposed while the group's aggregated
  availability keeps `staged` at zero no matter how high the demand
  climbs;
- drives the rise tick by tick through the active's monitor,
  recording the chain's own level input and asserting `demand`
  (the emitted model's declared chain output) moves 0→1→2 only at
  the declared crossings — `duty_call` with the `start` crossing,
  `lag_call` with the `lag_start` crossing — and the `high`
  crossing asserting `high_level` beside the managed high-level
  alarm's `alarm`/`unacknowledged`, the journaled annunciation the
  durable record must carry;
- restores the driven inputs — the receipted `oos` releases settling
  `applied` — so the standing demand stages the group: the duty pump
  answers first, the lag follows inside the declared
  `start_delay_ticks`, and each pump's `cmd`/`run` field outputs
  prove the delivered start;
- lets the staged pumps draw the level down, asserting the falling
  edge de-stages in the declared order — the demand falling at its
  `start`/`stop` crossings while the most recently staged lag's
  command releases before the duty's — down to the `below_cutoff`
  floor the journaled chain flag reports;
- clears the alarm's unacknowledged latch through a receipted `ack`
  write and audits the record: the field owner's durable journal
  file must carry each driven transition and attributed settlement
  in `seq` order — the holds, the high-level annunciation, the
  releases, the staged runs, and the lag-first de-stage — the
  served `GET /journal` answering the same record, every driven
  input restored and the pair's roles unchanged throughout.

Usage:

    staging.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `staging-digest <sha256>` line prints — the check runs
two passes and compares them (`staging-nondeterministic`). A contract
violation reports `staging: …` lines on stderr and exits 1 — the
check's `staging-failed`. `--tamper wrong-demand` doctors the leg's
own expectation at the `lag_start` crossing — asserting the demand
still reads the duty stage — and `--tamper immediate-lag` asserts the
lag start landed inside a shortened bound, so the leg proves its
crossing and delay assertions fire rather than passing an unexercised
contract.
"""

import argparse
import hashlib
import json
import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "legs")
)

import pair
import simulate


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The driven ticks each phase's declared effect gets to land: the
# unopposed rise's bound — the declared inflow lifts the level through
# the whole chain — and the bound each receipted write's effect gets
# across the wiring's one-scan carrier crossings. The actor the leg's
# receipted submissions declare.
RISE_BOUND = 24
SETTLE_BOUND = 16
ACTOR = "ci-staging"

# The signal names resolving the leg's point ids out of the emitted
# model — the same names the signal index serves, so the leg exercises
# the declared seam, never a hard-coded id.
SIGNALS = {
    "level_selected": "level-selected",
    "level_chain_in": "level-chain-in",
    "demand": "demand",
    "demand_in": "demand-in",
    "duty": "duty",
    "staged": "staged",
    "duty_call": "duty-call",
    "lag_call": "lag-call",
    "below_cutoff": "below-cutoff",
    "high_level": "high-level",
    "none_available": "none-available",
    "oos_1": "p101-oos",
    "oos_2": "p102-oos",
    "avail_1": "p101-avail",
    "avail_2": "p102-avail",
    "cmd_1": "p101-cmd",
    "cmd_2": "p102-cmd",
    "run_1": "p101-run",
    "run_2": "p102-run",
    "lah_alarm": "lah-alarm",
    "lah_unack": "lah-unacknowledged",
    "lah_ack": "lah-ack",
}


def signal_points(model):
    """The `{key: point id}` map the emitted model's signal index
    declares for the leg's driven and reported points — None when the
    model declares no such staging seam. A signal's `source` is the
    point it names; the lowest-signal-id-wins rule the served index
    applies."""
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
    for key in ("oos_1", "oos_2", "lah_ack"):
        if points[key] not in writable:
            return None
    return points


def declared_parameters(model):
    """The declared setpoint chain and start delay the emitted model
    carries — `(setpoints, start_delay_ticks)` resolved from the
    `threshold-chain` and `pump-group` components' parameter maps, or
    None when the model declares no such pair. The leg asserts against
    the declared values, never a copied table."""
    setpoints = start_delay = None
    for component in model.get("components", []):
        parameters = component.get("parameters", {})
        if component["kind"] == "threshold-chain":
            try:
                setpoints = {
                    name: parameters[name]["float"]
                    for name in ("cutoff", "stop", "start", "lag_start", "high")
                }
            except KeyError:
                return None
        elif component["kind"] == "pump-group":
            start_delay = parameters.get("start_delay_ticks", {}).get("int")
    if setpoints is None or start_delay is None:
        return None
    return setpoints, start_delay


def value(snapshot, point):
    """The point's latest sample value — `simulate.snapshot_point`."""
    return simulate.snapshot_point(snapshot, point)


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


def tick(rig, failures):
    """One driven pair tick — the harness's tracking-first scan,
    identical images asserted — returns the owner's served
    snapshot."""
    return rig.tick(rig.standby_url, rig.duty_url, failures)[1]


def run_ticks(rig, failures, count):
    """`count` driven pair ticks; returns the last owner snapshot."""
    owner = None
    for _ in range(count):
        owner = tick(rig, failures)
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


def observe(owner, points):
    """The per-tick observation the crossing assertions read — the
    chain's own level input beside its outputs and the group's
    answers, all as served values."""
    observed = {"tick": owner["tick"]}
    for key, point in points.items():
        observed[key] = value(owner, point)
    return observed


def journal_events(entries):
    """The leg's audit stream out of a journal entry list —
    `("settled", point, value, outcome, actor)` for each command
    receipt, `("changed", point, to)` for each journaled value
    transition, and `("quality", point, to)` for each quality
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
        elif "quality_changed" in event:
            change = event["quality_changed"]
            events.append(("quality", change.get("point"), change.get("to")))
    return events


def ordered_group_misses(events, groups):
    """The ordered-audit check: each group's events must appear in the
    journal's `seq` order after the previous group's — the leg's
    attributed transitions landing in run order. Returns the named
    misses."""
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


def first_where(observed, condition):
    """The first per-tick observation satisfying `condition`, or
    None — the crossing-detection the demand assertions use."""
    return next((entry for entry in observed if condition(entry)), None)


def demand_at(observed):
    """The observed demand as an int, 0 when unsampled."""
    demand = observed.get("demand") or {}
    return demand.get("int", 0)


def level_at(observed):
    """The observed chain-input level as a float, NaN when
    unsampled."""
    level = observed.get("level_chain_in") or {}
    return level.get("float", float("nan"))


def flag(observed, key):
    """The observed Bool point as a truth value."""
    reading = observed.get(key) or {}
    return reading.get("bool") is True


def staging_pass(args, tamper):
    """The staging run: hold, rise, release, stage, draw down, de-stage,
    acknowledge, audit. Returns `(digest_entries, evidence,
    failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the staging leg "
            "has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    with open(args.model) as handle:
        model = json.load(handle)
    points = signal_points(model)
    if points is None:
        raise Abort(
            "the emitted model declares no writable out-of-service "
            "seam — the staging leg has nothing to exercise"
        )
    parameters = declared_parameters(model)
    if parameters is None:
        raise Abort(
            "the emitted model declares no threshold-chain/pump-group "
            "pair — the staging leg has nothing to exercise"
        )
    setpoints, start_delay = parameters
    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        duty_files = rig.duty_files

        # Phase 1 — the hold: receipted `write_value` on each pump's
        # declared `oos` point, submitted before the first driven tick
        # so the writes apply at the opening scan — with every pump
        # held out of service the declared inflow raises the level
        # unopposed and the group's aggregated availability keeps the
        # stage count at zero whatever the demand reads.
        holds = []
        for key in ("oos_1", "oos_2"):
            command = write_value(points[key], True)
            receipt = submit(duty_url, command, failures)
            holds.append({"command": command, "receipt": receipt})

        # Phase 2 — the unopposed rise: driven ticks recording the
        # chain's own level input and outputs each scan until the
        # `high` crossing annunciates. The demand must move 0→1→2 only
        # at the declared crossings — never ahead of the level the
        # chain actually read — while `staged` and every `cmd`/`run`
        # stay released under the hold.
        rise = []
        high_at = None
        for _ in range(RISE_BOUND):
            owner = tick(rig, failures)
            observed = observe(owner, points)
            rise.append(observed)
            if (
                flag(observed, "high_level")
                and flag(observed, "lah_alarm")
                and flag(observed, "lah_unack")
            ):
                high_at = owner["tick"]
                break
        if high_at is None:
            failures.append(
                "the unopposed rise never reached the high crossing — "
                f"the last observation reads {rise[-1] if rise else 'nothing'}"
            )
            raise Abort
        if demand_at(rise[0]) != 0:
            failures.append(
                f"the run did not begin unstaged — the first "
                f"observation reads {rise[0]}"
            )
            raise Abort
        # The `start` crossing: the first observed demand at 1 must
        # read the chain input at or above `start`, `duty_call`
        # reporting with it, and no earlier tick may have shown the
        # demand ahead of its level.
        start_crossing = first_where(rise, lambda entry: demand_at(entry) >= 1)
        if start_crossing is None or not (
            level_at(start_crossing) >= setpoints["start"]
        ):
            failures.append(
                f"the demand first staged {start_crossing} — the "
                f"`start` crossing at {setpoints['start']} never "
                "produced it, or the demand ran ahead of the level "
                "the chain read"
            )
            raise Abort
        if not flag(start_crossing, "duty_call"):
            failures.append(
                f"the `start` crossing left duty_call released — the "
                f"crossing observation reads {start_crossing}"
            )
            raise Abort
        jumped = next(
            (
                entry
                for entry in rise
                if demand_at(entry) >= 1 and level_at(entry) < setpoints["start"]
            ),
            None,
        )
        if jumped is not None:
            failures.append(
                f"the demand staged ahead of the `start` crossing — "
                f"{jumped} shows demand with the chain input below "
                f"{setpoints['start']}"
            )
            raise Abort
        # The `lag_start` crossing: the first observed demand at 2
        # must read the chain input at or above `lag_start`,
        # `lag_call` reporting with it.
        lag_crossing = first_where(rise, lambda entry: demand_at(entry) >= 2)
        want_demand = 1 if tamper == "wrong-demand" else 2
        if lag_crossing is None or not (
            demand_at(lag_crossing) == want_demand
            and level_at(lag_crossing) >= setpoints["lag_start"]
        ):
            failures.append(
                f"the `lag_start` crossing at {setpoints['lag_start']} "
                f"produced {lag_crossing}, expected the demand at "
                f"{want_demand} on a level at or above it"
            )
            raise Abort
        if not flag(lag_crossing, "lag_call"):
            failures.append(
                f"the `lag_start` crossing left lag_call released — "
                f"the crossing observation reads {lag_crossing}"
            )
            raise Abort
        # The hold's proof: the whole rise ran with nothing staged —
        # the demand the group could not fill climbed past every
        # crossing while `staged` and the motor commands stayed
        # released.
        cheated = next(
            (
                entry
                for entry in rise
                if (entry.get("staged") or {}).get("int", 0) != 0
                or flag(entry, "cmd_1")
                or flag(entry, "cmd_2")
                or flag(entry, "run_1")
                or flag(entry, "run_2")
            ),
            None,
        )
        if cheated is not None:
            failures.append(
                f"a pump staged under the out-of-service hold — "
                f"{cheated} shows the group answering the demand it "
                "could not fill"
            )
            raise Abort
        evidence["held_at"] = holds[0]["receipt"]
        evidence["start_crossing"] = start_crossing["tick"]
        evidence["lag_crossing"] = lag_crossing["tick"]
        evidence["high_at"] = high_at
        digest_entries.append(
            {
                "phase": "rise",
                "holds": holds,
                "start_crossing": start_crossing,
                "lag_crossing": lag_crossing,
                "high": rise[-1],
            }
        )

        # The pair settled: the standby's role report must read
        # `tracking` and the field owner `active` — the run's
        # convergence evidence rides the rise's driven ticks.
        converged = rig.converge(failures, count=1)
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )

        # Phase 3 — the release: the receipted `oos` releases restore
        # the driven inputs, settling `applied` into the adopted log;
        # the aggregated availability returns and the standing demand
        # stages the group — the duty pump first, the lag inside the
        # declared `start_delay_ticks`, each pump's `cmd`/`run` field
        # outputs proving the delivered start.
        releases = []
        for key in ("oos_1", "oos_2"):
            command = write_value(points[key], False)
            receipt = submit(duty_url, command, failures)
            releases.append({"command": command, "receipt": receipt})
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: flag(observe(snapshot, points), "avail_1")
            and flag(observe(snapshot, points), "avail_2"),
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                "the out-of-service releases never returned the pumps' "
                f"availability — avail reads "
                f"{value(owner, points['avail_1'])} / "
                f"{value(owner, points['avail_2'])}"
            )
            raise Abort
        available_at = owner["tick"]
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
        for entry in holds + releases:
            if not settled(receipts_duty, entry["command"]):
                failures.append(
                    f"the write {entry['command']} never settled "
                    "applied into the adopted receipt log"
                )
        if failures:
            raise Abort
        # The starts: the duty pump's command answers first, the lag
        # follows inside the declared start delay — measured on the
        # group's own `staged` report so the field carriers' hops do
        # not count against the bound.
        staged = []
        for _ in range(SETTLE_BOUND):
            owner = tick(rig, failures)
            staged.append(observe(owner, points))
            if (observe(owner, points)["staged"] or {}).get("int", 0) >= 2:
                break
        duty_start = first_where(
            staged, lambda entry: (entry["staged"] or {}).get("int", 0) >= 1
        )
        lag_start = first_where(
            staged, lambda entry: (entry["staged"] or {}).get("int", 0) >= 2
        )
        if duty_start is None or lag_start is None:
            failures.append(
                f"the standing demand never staged the group — the "
                f"observations read {staged[-1] if staged else 'nothing'}"
            )
            raise Abort
        delay_bound = start_delay if tamper != "immediate-lag" else start_delay - 1
        gap = lag_start["tick"] - duty_start["tick"]
        if gap > delay_bound or gap < 0:
            failures.append(
                f"the lag start landed {gap} ticks after the duty "
                f"start, outside the declared start_delay_ticks bound "
                f"{delay_bound}"
            )
            raise Abort
        # The group's own duty report names the holder — the same
        # order the falling edge unwinds: the duty pump staged first,
        # the lag second.
        reported_duty = (lag_start.get("duty") or {}).get("int", 0)
        if reported_duty not in (1, 2):
            failures.append(
                f"the group's duty report reads {lag_start.get('duty')} "
                "at staging — expected a held duty designation"
            )
            raise Abort
        lag_index = 3 - reported_duty
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: flag(observe(snapshot, points), f"cmd_{reported_duty}")
            and flag(observe(snapshot, points), f"cmd_{lag_index}")
            and flag(observe(snapshot, points), f"run_{reported_duty}")
            and flag(observe(snapshot, points), f"run_{lag_index}"),
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                f"the staged starts never proved at the field — cmd "
                f"reads {value(owner, points['cmd_1'])} / "
                f"{value(owner, points['cmd_2'])}, run "
                f"{value(owner, points['run_1'])} / "
                f"{value(owner, points['run_2'])}"
            )
            raise Abort
        if demand_at(observe(owner, points)) != 2:
            failures.append(
                f"the demand released before both pumps proved — the "
                f"observation reads {observe(owner, points)}"
            )
            raise Abort
        evidence["staged_at"] = lag_start["tick"]
        digest_entries.append(
            {
                "phase": "stage",
                "releases": releases,
                "available_at": available_at,
                "duty_start": duty_start,
                "lag_start": lag_start,
                "gap": gap,
                "duty": reported_duty,
                "running": observe(owner, points),
            }
        )

        # Phase 4 — the drawdown: the staged pumps pull the level down
        # through the chain's falling edge — `demand` releases the lag
        # at its `start` crossing and ends the pump-down at `stop`,
        # the most recently staged pump's command releasing before the
        # duty's while `staged` walks 2→1→0. The journaled `run`
        # releases must land in the same lag-first order.
        fall = []
        stopped_at = None
        for _ in range(SETTLE_BOUND):
            owner = tick(rig, failures)
            observed = observe(owner, points)
            fall.append(observed)
            if (
                demand_at(observed) == 0
                and not flag(observed, "run_1")
                and not flag(observed, "run_2")
            ):
                stopped_at = owner["tick"]
                break
        if stopped_at is None:
            failures.append(
                f"the pump-down never reached the declared floor — "
                f"the last observation reads {fall[-1] if fall else 'nothing'}"
            )
            raise Abort
        if not flag(fall[-1], "below_cutoff"):
            failures.append(
                f"the pumped-down well never reported the declared "
                f"floor — below_cutoff reads "
                f"{value(owner, points['below_cutoff'])}"
            )
            raise Abort
        if (
            (fall[-1]["staged"] or {}).get("int", 0) != 0
            or (fall[-1]["demand_in"] or {}).get("int", 0) != 0
        ):
            failures.append(
                f"the group did not unwind with the demand — the "
                f"final observation reads {fall[-1]}"
            )
            raise Abort
        # The falling crossings: the demand may not release ahead of
        # the level the chain read — the first demand below 2 must
        # read the input at or below `start`, the first at 0 at or
        # below `stop`.
        release_crossing = first_where(
            fall, lambda entry: demand_at(entry) <= 1
        )
        if release_crossing is None or not (
            level_at(release_crossing) <= setpoints["start"]
        ):
            failures.append(
                f"the lag released ahead of its `start` crossing — "
                f"{release_crossing} shows the demand falling while "
                f"the chain input still read above {setpoints['start']}"
            )
            raise Abort
        stop_crossing = first_where(fall, lambda entry: demand_at(entry) == 0)
        if stop_crossing is None or not (
            level_at(stop_crossing) <= setpoints["stop"]
        ):
            failures.append(
                f"the pump-down ended ahead of the `stop` crossing — "
                f"{stop_crossing} shows the demand released while "
                f"the chain input still read above {setpoints['stop']}"
            )
            raise Abort
        # Nor may the demand hold past a crossing — the lag must
        # release at `start`, the last stage at `stop`.
        held = next(
            (
                entry
                for entry in fall
                if demand_at(entry) >= 2 and level_at(entry) <= setpoints["start"]
            ),
            None,
        )
        if held is not None:
            failures.append(
                f"the lag held past its `start` crossing — {held} "
                f"shows the demand at 2 with the chain input at or "
                f"below {setpoints['start']}"
            )
            raise Abort
        held = next(
            (
                entry
                for entry in fall
                if demand_at(entry) >= 1 and level_at(entry) <= setpoints["stop"]
            ),
            None,
        )
        if held is not None:
            failures.append(
                f"the duty held past the `stop` crossing — {held} "
                f"shows the demand at 1 with the chain input at or "
                f"below {setpoints['stop']}"
            )
            raise Abort
        # The de-stage order: the lag — the most recently staged — is
        # the first `cmd`/`run` pair released, the duty holder's last.
        lag_release = first_where(
            fall, lambda entry: not flag(entry, f"run_{lag_index}")
        )
        duty_release = first_where(
            fall, lambda entry: not flag(entry, f"run_{reported_duty}")
        )
        if (
            lag_release is None
            or duty_release is None
            or not lag_release["tick"] < duty_release["tick"]
        ):
            failures.append(
                f"the de-stage order inverted — the lag's run "
                f"released at {lag_release}, the duty's at "
                f"{duty_release}, expected the lag first"
            )
            raise Abort
        single = first_where(
            fall,
            lambda entry: (entry["staged"] or {}).get("int", 0) == 1,
        )
        if single is not None and (single["demand_in"] or {}).get("int", 0) != 1:
            failures.append(
                f"the single staged pump stood while the group's "
                f"demand read {single.get('demand_in')} — the "
                "de-stage did not follow the falling demand"
            )
            raise Abort
        evidence["stopped_at"] = stopped_at
        digest_entries.append(
            {
                "phase": "drawdown",
                "release_crossing": release_crossing,
                "stop_crossing": stop_crossing,
                "single_staged": single,
                "lag_release": lag_release,
                "duty_release": duty_release,
                "stopped": fall[-1],
            }
        )

        # Phase 5 — the restore: a receipted `ack` clears the
        # high-level alarm's unacknowledged latch, then releases; the
        # driven inputs all stand restored and the pair's roles never
        # moved.
        ack_write = write_value(points["lah_ack"], True)
        receipt = submit(duty_url, ack_write, failures)
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: not flag(observe(snapshot, points), "lah_unack"),
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                f"the receipted ack never cleared the latch — "
                f"unacknowledged reads "
                f"{value(owner, points['lah_unack'])}"
            )
            raise Abort
        ack_release = write_value(points["lah_ack"], False)
        submit(duty_url, ack_release, failures)
        owner = run_ticks(rig, failures, 1)
        for key in ("oos_1", "oos_2"):
            if flag(observe(owner, points), key):
                failures.append(
                    f"the {key} hold was never restored — it reads "
                    f"{value(owner, points[key])}"
                )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "restore",
                "ack_receipt": receipt,
                "unacknowledged": value(owner, points["lah_unack"]),
                "oos_1": value(owner, points["oos_1"]),
                "oos_2": value(owner, points["oos_2"]),
            }
        )

        # Phase 6 — the audit: the pair's roles never moved, and the
        # field owner's durable journal file carries every driven
        # transition and attributed settlement in `seq` order — the
        # served journal answering the same record.
        standby_role = pair.get(f"{standby_url}/role", "GET /role", failures)
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        sync = standby_role.get("sync")
        if standby_role.get("role") != "standby" or not (
            isinstance(sync, dict) and "tracking" in sync
        ):
            failures.append(
                "the standby's role moved through the staging run — "
                f"GET /role answers {standby_role}"
            )
        if duty_role.get("role") != "active":
            failures.append(
                f"the field owner's role moved through the staging "
                f"run — GET /role answers {duty_role}"
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

        true = {"bool": True}
        false = {"bool": False}
        groups = [
            # The hold: both `oos` writes settled applied and the
            # journaled holds, availability drops, and none-available
            # condition landing in driven order.
            [
                ("settled", points["oos_1"], true, "applied", ACTOR),
                ("settled", points["oos_2"], true, "applied", ACTOR),
                ("changed", points["oos_1"], true),
                ("changed", points["oos_2"], true),
                ("changed", points["avail_1"], false),
                ("changed", points["avail_2"], false),
                ("changed", points["none_available"], true),
            ],
            # The `high` crossing's annunciation — the journaled
            # evidence the managed high-level alarm owes.
            [
                ("changed", points["high_level"], true),
                ("changed", points["lah_alarm"], true),
                ("changed", points["lah_unack"], true),
            ],
            # The releases: each restore settled applied beside the
            # returned holds, availability, and station condition.
            [
                ("settled", points["oos_1"], false, "applied", ACTOR),
                ("settled", points["oos_2"], false, "applied", ACTOR),
                ("changed", points["oos_1"], false),
                ("changed", points["oos_2"], false),
                ("changed", points["avail_1"], true),
                ("changed", points["avail_2"], true),
                ("changed", points["none_available"], false),
            ],
            # The staged starts proven at the field — the duty's run
            # feedback first, the lag's second.
            [
                ("changed", points[f"run_{reported_duty}"], true),
                ("changed", points[f"run_{lag_index}"], true),
            ],
            # The falling edge: the alarm returns, the chain reports
            # the pumped-down floor, and the de-stage releases the lag
            # before the duty.
            [
                ("changed", points["high_level"], false),
                ("changed", points["lah_alarm"], false),
                ("changed", points["below_cutoff"], true),
                ("changed", points[f"run_{lag_index}"], false),
                ("changed", points[f"run_{reported_duty}"], false),
            ],
            # The receipted ack clearing the latch and releasing.
            [
                ("settled", points["lah_ack"], true, "applied", ACTOR),
                ("changed", points["lah_unack"], false),
                ("settled", points["lah_ack"], false, "applied", ACTOR),
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
        choices=["wrong-demand", "immediate-lag"],
        help="doctor the leg's own expectation — the pass must fail "
        "naming the actual crossing or staging evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = staging_pass(args, args.tamper)
    except Abort as abort:
        for line in abort.args:
            eprint(f"staging: {line}")
        return 1
    for failure in failures:
        eprint(f"staging: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"staging: the {args.tamper} case passed silently — "
                "the leg never noticed the doctored expectation"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"staging-digest {digest} — demand 1 at tick "
        f"{evidence['start_crossing']}, 2 at tick "
        f"{evidence['lag_crossing']}, high annunciated at tick "
        f"{evidence['high_at']}, staged at tick "
        f"{evidence['staged_at']}, pumped down by tick "
        f"{evidence['stopped_at']}, run continued to tick "
        f"{evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
