#!/usr/bin/env python3
"""The pair contract's duty-pump failure-handover leg — decision 41's
central behavior proven on the deployed consumer pair (WW-ENG-003,
WW-CTL-001, WW-OPS-001).

With the pair settled — the tracking peer applying checkpoints, the
field owner active — and the pump group holding a duty demand with the
duty pump proven running, the leg faults the duty pump's run-feedback
field channel through the plant protocol's declared `inject_fault` —
the same honest lever the scripted scenario's plant requests use —
then drives the pair and asserts through the active's monitor:

* `duty` moves to the standby pump inside the declared bound — the
  motor's `fault_ticks` proof plus the carrier hop the group's
  `fault_i` read crosses — with `staged` reporting the surviving pump
  alone against the standing demand;
* the failed pump's `fault` reports the exclusion — the group excludes
  on its `fault_i` input, so `p101-fault` asserts while `p101-avail`,
  the station's five-leg availability aggregate, honestly keeps
  reporting the permissives it wires — and the managed `p101-fault`
  alarm annunciates (`alarm` + `unacknowledged`), the active's served
  journal carrying the `point_changed` transitions for the fault flag
  and the annunciation;
* faulting the remaining pump's channel then annunciates the all-out
  conditions — `none_available`, `all_faulted`, and both managed
  alarms — with `duty` reporting none and `staged` zero;
* clearing each injected fault restores the declared recovery — the
  fault flags clear once command and feedback agree, the all-out
  annunciation returns, the duty designation reassigns to the
  recovered pump under the declared rotation, and the alarm latches
  hold their unacknowledged records awaiting an acknowledgment the leg
  never sends;
* the pair's controller roles never move — a field fault is a plant
  event, not a controller failover — and the peers' images stay
  identical through every driven scan, the standby tracking the same
  faulted image it would adopt on a real switchover.

The `--tamper` cases doctor the leg's own expectations: `keeps-duty`
requires the failed pump to keep `duty`, and `none-available-silent`
requires `none_available` never to report — each must fail naming the
evidence.

On success one `handover-digest <sha256>` line prints — the check runs
two passes and requires identical digests.
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

CONVERGE_TICKS = pair.CONVERGE_TICKS

# The declared bounds, in driven scans. The motor's `fault_ticks` proof
# needs two disagreeing reads of the faulted channel and the group's
# `fault_i` exclusion lands on the scan delivering the proven — or
# already non-Good — flag, so `duty` must move within three scans of
# the injection and the managed alarm's carrier hop must annunciate
# within four; the leg drives four per window and asserts each
# transition landed inside them.
HANDOVER_BOUND = 3
WINDOW_SCANS = 4
# The demand wait: a settled cold-start pair reaches duty demand and
# proven run feedback well inside this many driven scans.
DEMAND_BOUND = 12

# The composition's named signals the leg exercises — resolved to point
# ids out of the emitted model so the leg speaks the station's names,
# not magic numbers.
SIGNALS = [
    "demand",
    "duty",
    "staged",
    "none-available",
    "all-faulted",
    "p101-run",
    "p102-run",
    "p101-fault",
    "p102-fault",
    "p101-avail",
    "p102-avail",
    "p101-fault-alarm",
    "p101-fault-unacknowledged",
    "p102-fault-alarm",
    "p102-fault-unacknowledged",
    "none-available-alarm",
    "none-available-unacknowledged",
    "all-faulted-alarm",
    "all-faulted-unacknowledged",
]


def signal_points(model_path):
    """The emitted model's point ids for the leg's named signals."""
    with open(model_path) as handle:
        model = json.load(handle)
    ids = {}
    for signal in model.get("signals", []):
        name = signal.get("name")
        if name in SIGNALS and name not in ids:
            ids[name] = signal["source"]
    missing = [name for name in SIGNALS if name not in ids]
    if missing:
        raise Abort(f"the emitted model declares no signals named {missing}")
    return ids


def sample_at(snapshot, point):
    """The point's latest served sample — value, quality, tick."""
    for entry in snapshot["points"]:
        if entry["point"] == point:
            return entry.get("sample")
    raise KeyError(point)


def bool_at(snapshot, point):
    value = simulate.snapshot_point(snapshot, point)
    return value.get("bool") if isinstance(value, dict) else None


def int_at(snapshot, point):
    value = simulate.snapshot_point(snapshot, point)
    return value.get("int") if isinstance(value, dict) else None


def fault_channel(client, point, failures):
    """Fault one field channel through the plant protocol's declared
    `inject_fault` — bad quality the controller's reads surface, the
    honest failure the emitted dynamics' fault vocabulary supports."""
    response = client.request(
        {
            "op": "inject_fault",
            "point": point,
            "fault": {"quality": {"bad": "device_fault"}},
        }
    )
    if response.get("result") != "done":
        failures.append(f"inject_fault on point {point} answered {response}")
        raise Abort


def clear_channel(client, point, failures):
    """Remove an injected channel fault — the field input restored."""
    response = client.request({"op": "clear_fault", "point": point})
    if response.get("result") != "done":
        failures.append(f"clear_fault on point {point} answered {response}")
        raise Abort


def point_transitions(entries, point):
    """The `point_changed` transitions a served journal entry list
    carries for one point — `(tick, from, to)` in `seq` order."""
    return [
        (entry["tick"], change["from"], change["to"])
        for entry in entries
        if "point_changed" in entry.get("event", {})
        for change in [entry["event"]["point_changed"]]
        if change.get("point") == point
    ]


def has_transition(entries, point, before, after):
    """Whether the journal records the point's `before` -> `after`
    `point_changed` transition."""
    return any(
        start == before and end == after
        for _tick, start, end in point_transitions(entries, point)
    )


def handover_pass(args, tamper):
    """The handover run: settle, fault, all-out, restore, record.
    Returns `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the handover leg "
            "has nothing to exercise"
        )
    manifest, duty_decl, standby_decl = declared
    ids = signal_points(args.model)
    scratch = tempfile.mkdtemp(prefix="dcs-handover-")
    digest_entries, evidence, failures = [], {}, []
    plant = duty = standby = None
    try:

        def persistence(entry):
            """The manifest's container persistence paths instantiated
            under the leg's runner-owned scratch directory."""
            root = os.path.join(scratch, entry["name"])
            os.makedirs(root, exist_ok=True)
            return {
                field: os.path.join(root, os.path.basename(entry[field]))
                if entry.get(field)
                else None
                for field in ("state_file", "journal_file")
            }

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
            args.controller,
            args.model,
            args.dt,
            plant_addr,
            None,
            persistence(duty_decl),
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
            persistence(standby_decl),
        )
        if standby_url is None:
            raise Abort(
                f"the standby controller {standby_decl['name']} exited "
                f"at startup: {'; '.join(preamble) or 'no diagnostic'}"
            )

        def tick():
            """One driven pair scan — the tracking peer first, its
            request pulling and applying the owner's latest checkpoint —
            with the pair's image identity checked every scan. Returns
            the active's snapshot."""
            tracked = pair.scan(standby_url, failures)
            owner = pair.scan(duty_url, failures)
            if pair.select_snapshot(tracked) != pair.select_snapshot(owner):
                failures.append(
                    "the tracking peer's image diverged from the field "
                    f"owner's at tick {owner['tick']}"
                )
                raise Abort
            return owner

        # Phase 1 — settle: converge the standby, prove the roles, then
        # drive until the group holds a duty demand with pump 1 on duty
        # and its run feedback proven — the ticket's starting point.
        owner = None
        for _ in range(CONVERGE_TICKS):
            owner = tick()
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
        demand_tick = None
        for _ in range(DEMAND_BOUND):
            if (
                (int_at(owner, ids["demand"]) or 0) >= 1
                and int_at(owner, ids["duty"]) == 1
                and bool_at(owner, ids["p101-run"]) is True
            ):
                demand_tick = owner["tick"]
                break
            owner = tick()
        if demand_tick is None:
            failures.append(
                "the group never held a duty demand with pump 1 proven "
                "running — the leg's settled starting point never came"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "settle",
                "demand_tick": demand_tick,
                "demand": int_at(owner, ids["demand"]),
                "duty": int_at(owner, ids["duty"]),
                "staged": int_at(owner, ids["staged"]),
            }
        )

        # Phase 2 — the proven duty-pump failure: the p101-run channel
        # reads bad, the motor proves its fault, and the group hands
        # duty to the standby pump inside the declared bound.
        inject_tick = owner["tick"]
        fault_channel(plant_io, ids["p101-run"], failures)
        moved = annunciated = None
        trace = []
        for _ in range(WINDOW_SCANS):
            owner = tick()
            lag = owner["tick"] - inject_tick
            trace.append(
                {
                    "tick": owner["tick"],
                    "duty": int_at(owner, ids["duty"]),
                    "staged": int_at(owner, ids["staged"]),
                    "p101_fault": bool_at(owner, ids["p101-fault"]),
                    "p101_avail": bool_at(owner, ids["p101-avail"]),
                    "p101_run": sample_at(owner, ids["p101-run"]),
                    "alarm": bool_at(owner, ids["p101-fault-alarm"]),
                }
            )
            if moved is None and int_at(owner, ids["duty"]) == 2:
                moved = lag
            if annunciated is None and bool_at(
                owner, ids["p101-fault-alarm"]
            ) is True:
                annunciated = lag
        final = owner
        if tamper == "keeps-duty":
            if int_at(final, ids["duty"]) != 1:
                failures.append(
                    f"duty moved to pump {int_at(final, ids['duty'])} "
                    "after the proven p101 failure — the doctored leg "
                    "expected the failed pump to keep duty"
                )
        else:
            if moved is None:
                failures.append(
                    "duty never moved to the standby pump after the "
                    "proven p101 failure"
                )
            elif moved > HANDOVER_BOUND:
                failures.append(
                    f"duty took {moved} scans to reach the standby pump "
                    f"— the declared bound is {HANDOVER_BOUND}"
                )
        if int_at(final, ids["duty"]) != 2 and tamper != "keeps-duty":
            failures.append(
                f"duty reports {int_at(final, ids['duty'])} after the "
                "proven p101 failure, expected pump 2"
            )
        demand_now = int_at(final, ids["demand"]) or 0
        staged = int_at(final, ids["staged"])
        if staged != min(demand_now, 1):
            failures.append(
                f"staged reports {staged} with demand {demand_now} and "
                "one available pump — the surviving pump alone should "
                "be commanded"
            )
        if bool_at(final, ids["p101-fault"]) is not True:
            failures.append("p101-fault never reported the proven exclusion")
        if bool_at(final, ids["p101-avail"]) is not True:
            failures.append(
                "p101-avail stopped reporting its satisfied aggregate — "
                "the exclusion is the proven fault, not an availability "
                "collapse"
            )
        if bool_at(final, ids["p101-fault-alarm"]) is not True or bool_at(
            final, ids["p101-fault-unacknowledged"]
        ) is not True:
            failures.append(
                "the managed p101-fault alarm never annunciated"
            )
        if bool_at(final, ids["none-available"]) is not False:
            failures.append(
                "none_available reported with pump 2 still available"
            )
        digest_entries.append(
            {
                "phase": "handover",
                "inject_tick": inject_tick,
                "moved_in": moved,
                "annunciated_in": annunciated,
                "trace": trace,
            }
        )
        evidence["handed_over_at"] = (
            None if moved is None else inject_tick + moved
        )

        # Phase 3 — the all-out failure: the remaining pump's channel
        # faults, and the group's `none_available`/`all_faulted`
        # conditions annunciate with their managed alarms.
        allout_tick = owner["tick"]
        fault_channel(plant_io, ids["p102-run"], failures)
        trace = []
        for _ in range(WINDOW_SCANS):
            owner = tick()
            trace.append(
                {
                    "tick": owner["tick"],
                    "duty": int_at(owner, ids["duty"]),
                    "staged": int_at(owner, ids["staged"]),
                    "p102_fault": bool_at(owner, ids["p102-fault"]),
                    "p102_avail": bool_at(owner, ids["p102-avail"]),
                    "none_available": bool_at(owner, ids["none-available"]),
                    "all_faulted": bool_at(owner, ids["all-faulted"]),
                    "none_alarm": bool_at(owner, ids["none-available-alarm"]),
                    "all_alarm": bool_at(owner, ids["all-faulted-alarm"]),
                }
            )
        final = owner
        if tamper == "none-available-silent":
            if bool_at(final, ids["none-available"]) is True:
                failures.append(
                    "none_available annunciated once every pump's "
                    "availability dropped — the doctored leg expected "
                    "it never to report"
                )
        else:
            if bool_at(final, ids["none-available"]) is not True:
                failures.append(
                    "none_available never reported once every pump's "
                    "availability dropped"
                )
            if bool_at(final, ids["all-faulted"]) is not True:
                failures.append(
                    "all_faulted never reported with both pumps faulted"
                )
        if bool_at(final, ids["p102-fault"]) is not True:
            failures.append("p102-fault never reported its proven failure")
        if int_at(final, ids["duty"]) != 0:
            failures.append(
                f"duty reports {int_at(final, ids['duty'])} with no "
                "pump available, expected 0"
            )
        if int_at(final, ids["staged"]) != 0:
            failures.append(
                f"staged reports {int_at(final, ids['staged'])} with no "
                "pump available, expected 0"
            )
        for name in (
            "none-available-alarm",
            "none-available-unacknowledged",
            "all-faulted-alarm",
            "all-faulted-unacknowledged",
        ):
            if bool_at(final, ids[name]) is not True:
                failures.append(
                    f"the managed {name} never annunciated the all-out "
                    "condition"
                )
        digest_entries.append(
            {"phase": "all-out", "inject_tick": allout_tick, "trace": trace}
        )

        # Phase 4 — restore each input: p101's channel first. The
        # declared recovery — the fault flag clears once command and
        # feedback agree, the availability aggregate stays satisfied,
        # the all-out conditions drop with one pump back, the duty
        # designation reassigns to the recovered pump under the
        # declared rotation, the alarm returns, and the unacknowledged
        # latch holds.
        clear_channel(plant_io, ids["p101-run"], failures)
        for _ in range(WINDOW_SCANS):
            owner = tick()
        final = owner
        for name, want, what in (
            ("p101-fault", False, "p101's proven fault never cleared"),
            ("p101-avail", True, "p101-avail stopped reporting"),
            (
                "none-available",
                False,
                "none_available held after p101 recovered",
            ),
            (
                "all-faulted",
                False,
                "all_faulted held after p101's fault cleared",
            ),
            (
                "p101-fault-alarm",
                False,
                "the p101-fault alarm never returned",
            ),
            (
                "p101-fault-unacknowledged",
                True,
                "the p101-fault latch dropped without an acknowledgment",
            ),
            (
                "none-available-alarm",
                False,
                "the none_available alarm never returned",
            ),
            (
                "all-faulted-alarm",
                False,
                "the all_faulted alarm never returned",
            ),
            (
                "p102-fault",
                True,
                "p102's fault cleared before its channel restored",
            ),
        ):
            if bool_at(final, ids[name]) is not want:
                failures.append(what)
        if int_at(final, ids["duty"]) != 1:
            failures.append(
                f"duty reports {int_at(final, ids['duty'])} after p101 "
                "recovered — the declared rotation should hand the "
                "designation to pump 1"
            )
        demand_now = int_at(final, ids["demand"]) or 0
        staged = int_at(final, ids["staged"])
        if staged != min(demand_now, 1):
            failures.append(
                f"staged reports {staged} with demand {demand_now} and "
                "one available pump"
            )

        # Then p102's channel — the pair of pumps whole again, the duty
        # designation undisturbed, the second fault's annunciation
        # returned, its latch held.
        clear_channel(plant_io, ids["p102-run"], failures)
        for _ in range(WINDOW_SCANS):
            owner = tick()
        final = owner
        for name, want, what in (
            ("p102-fault", False, "p102's proven fault never cleared"),
            ("p102-avail", True, "p102-avail stopped reporting"),
            ("p102-fault-alarm", False, "the p102-fault alarm never returned"),
            (
                "p102-fault-unacknowledged",
                True,
                "the p102-fault latch dropped without an acknowledgment",
            ),
            (
                "none-available-unacknowledged",
                True,
                "the none_available latch dropped without an acknowledgment",
            ),
            (
                "all-faulted-unacknowledged",
                True,
                "the all_faulted latch dropped without an acknowledgment",
            ),
        ):
            if bool_at(final, ids[name]) is not want:
                failures.append(what)
        if int_at(final, ids["duty"]) != 1:
            failures.append(
                f"duty reports {int_at(final, ids['duty'])} with both "
                "pumps recovered — pump 1's designation should stand"
            )
        demand_now = int_at(final, ids["demand"]) or 0
        staged = int_at(final, ids["staged"])
        if staged != min(demand_now, 2):
            failures.append(
                f"staged reports {staged} with demand {demand_now} and "
                "both pumps available"
            )
        digest_entries.append(
            {
                "phase": "recover",
                "clear_tick": allout_tick + WINDOW_SCANS,
                "duty": int_at(final, ids["duty"]),
                "staged": staged,
                "demand": demand_now,
            }
        )

        # Phase 5 — the record: the pair's controller roles never moved
        # — a field fault is a plant event, not a failover — and the
        # active's served journal carries the `point_changed` evidence
        # for the fault, the annunciations, and the returns, with the
        # unacknowledged latches never dropping.
        standby_role = pair.get(f"{standby_url}/role", "GET /role", failures)
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        sync = standby_role.get("sync")
        if standby_role.get("role") != "standby" or not (
            isinstance(sync, dict) and "tracking" in sync
        ):
            failures.append(
                f"the tracking peer's role moved — GET /role answers "
                f"{standby_role} after the field faults"
            )
        if duty_role.get("role") != "active":
            failures.append(
                f"the field owner reports {duty_role.get('role')!r} "
                "after the field faults, expected active"
            )
        duty_journal = pair.get(
            f"{duty_url}/journal", "GET /journal", failures
        )
        standby_journal = pair.get(
            f"{standby_url}/journal", "GET /journal", failures
        )
        for name, entries in (
            (duty_decl["name"], duty_journal),
            (standby_decl["name"], standby_journal),
        ):
            transitions = pair.role_transitions(entries)
            if transitions:
                failures.append(
                    f"{name}'s journal carries role changes "
                    f"{transitions} — a field fault must not move the "
                    "pair's roles"
                )
        journaled = {}
        for name in (
            "p101-fault",
            "p101-fault-alarm",
            "p101-fault-unacknowledged",
            "p102-fault",
            "p102-fault-alarm",
            "p102-fault-unacknowledged",
            "none-available",
            "all-faulted",
            "none-available-alarm",
            "none-available-unacknowledged",
            "all-faulted-alarm",
            "all-faulted-unacknowledged",
        ):
            if has_transition(
                duty_journal, ids[name], {"bool": False}, {"bool": True}
            ):
                journaled[name] = point_transitions(duty_journal, ids[name])
            else:
                failures.append(
                    f"the active's journal carries no point_changed "
                    f"record of {name} asserting"
                )
        for name in (
            "p101-fault",
            "p102-fault",
            "none-available",
            "all-faulted",
            "p101-fault-alarm",
            "p102-fault-alarm",
            "none-available-alarm",
            "all-faulted-alarm",
        ):
            if not has_transition(
                duty_journal, ids[name], {"bool": True}, {"bool": False}
            ):
                failures.append(
                    f"the active's journal carries no point_changed "
                    f"record of {name} returning"
                )
        for name in (
            "p101-fault-unacknowledged",
            "p102-fault-unacknowledged",
            "none-available-unacknowledged",
            "all-faulted-unacknowledged",
        ):
            if has_transition(
                duty_journal, ids[name], {"bool": True}, {"bool": False}
            ):
                failures.append(
                    f"{name} dropped without an acknowledgment — the "
                    "managed latch must hold"
                )
        digest_entries.append(
            {
                "phase": "record",
                "journaled": journaled,
                "duty_role": duty_role,
                "standby_role": standby_role,
            }
        )
        evidence["final_tick"] = final["tick"]
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
        choices=["keeps-duty", "none-available-silent"],
        help="doctor the leg's expectations — the pass must fail "
        "naming the evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = handover_pass(args, args.tamper)
    except Abort as abort:
        for line in abort.args:
            eprint(f"handover: {line}")
        return 1
    for failure in failures:
        eprint(f"handover: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"handover: the {args.tamper} case passed silently — the "
                "leg never noticed the doctored expectation"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"handover-digest {digest} — duty handed to the standby pump at "
        f"tick {evidence.get('handed_over_at')}, all-out annunciated and "
        f"inputs restored by tick {evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
