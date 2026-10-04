#!/usr/bin/env python3
"""The low-level cutoff leg for the reference plant — the consumer-side
proof that the emitted model's declared dry-run clamp and the managed
low-level alarm's two-flag lifecycle hold on the deployed redundant
pair (WW-ENG-003, WW-CTL-002, WW-OPS-002).

The pair leg (`ci/legs/pair.py`) proves the manifest-declared pair runs
and switches; the takeover leg proves the manual leg's own cutoff
protection trips; the staging leg proves the upward chain. What no leg
proves is the chain's `cutoff` bound itself — the dry-run floor that
releases every call while a Good level reads at or below it — and the
`lal` low-level alarm's declared lifecycle against it. The rig reads
the standby wiring and persistence fields out of `deploy/manifest.json`
and spawns the released tooling exactly as the pair legs do. The run:

- converges the declared standby to `tracking` through the pair leg's
  driven-tick loop, then drives the simulated plant until the pump
  group holds a duty demand — a running duty pump the cutoff must
  stop;
- takes the duty pump to manual through the receipted command path —
  `p101-mode`/`p101-hand` `write_value` submissions on the active's
  `POST /command` — so the held operator demand draws the well down
  past the declared `stop` crossing where the group demand releases;
- asserts at the `cutoff` landing that `below_cutoff` rises, `demand`
  reads `0` with `duty_call`/`lag_call` off, every pump call is off,
  the hand-running pump's proven run releases on the protection trip,
  and `lal`'s `alarm`/`unacknowledged` annunciate — each transition
  journaled;
- holds the served wet-well level at the clamp through the plant
  protocol's writer seam — `ensure_writer` under the field owner's
  recorded owner token, the same shared-claim surface the scenario
  legs drive — because the alarm's `alarm` output follows the live
  condition and the refilling well would clear it inside one coarse
  integrator step: the receipted `lal-ack` must clear
  `unacknowledged` while `alarm` still stands;
- walks the held level back through the declared hysteresis —
  `alarm` standing inside the band, returning past it — then lets the
  field's own level rise to `start`, asserting `demand` resumes and a
  pump call stands again;
- restores every driven input — the mode/hand writes released and
  settled, the field writes dropped — and audits the record: the
  active's served `GET /journal` must carry each receipted write's
  `applied` settlement attributed to the leg's actor beside the
  journaled cutoff, alarm, and recovery transitions, in the run's
  `seq` order, with the pair's roles never moved throughout.

Usage:

    cutoff.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `cutoff-digest <sha256>` line prints — the check runs
two passes and compares them (`cutoff-nondeterministic`). A contract
violation reports `cutoff: …` lines on stderr and exits 1 — the
check's `cutoff-failed`. The `--tamper` cases doctor the leg's own
expectations: `demand-held` requires `demand` to stay asserted below
`cutoff`, and `lal-silent` requires the low-level alarm never to
annunciate — each must fail naming the evidence.
"""

import argparse
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import pair
import simulate
import takeover


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored cases: a leg expecting `demand` to stay asserted at
# or below `cutoff`, or expecting the low-level alarm never to
# annunciate, must surface the named diagnostic — never a silently
# wrong pass.
LEG = {
    "order": 850,
    "title": "the low-level cutoff leg",
    "passes": "cutoff-leg",
    "tampers": [
        {
            "name": "demand-held",
            "passed": "a doctored held-demand expectation passed the cutoff leg",
            "missed": "the demand-held case did not report its named diagnostic",
            "evidence": ["demand to stay asserted"],
        },
        {
            "name": "lal-silent",
            "passed": "a doctored silent-lal expectation passed the cutoff leg",
            "missed": "the lal-silent case did not report its named diagnostic",
            "evidence": ["lal annunciated"],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The driven ticks each phase's bound allows — the duty-demand wait,
# the carrier-hop bound each receipted write's declared effect gets
# to land across, the manual draw-down to the declared cutoff, and
# the refill to the declared `start` on inflow alone. The actor the
# leg's receipted submissions declare.
DEMAND_BOUND = 24
SETTLE_BOUND = 16
DRAIN_BOUND = 40
RESUME_BOUND = 24
ACTOR = "ci-cutoff"

# The signal names resolving the leg's point ids out of the emitted
# model — the same names the signal index serves, so the leg speaks
# the declared seam, never hard-coded ids.
SIGNALS = {
    "level_primary": "level-primary",
    "level_backup": "level-backup",
    "demand": "demand",
    "duty": "duty",
    "staged": "staged",
    "duty_call": "duty-call",
    "lag_call": "lag-call",
    "below_cutoff": "below-cutoff",
    "mode": "p101-mode",
    "hand": "p101-hand",
    "cmd": "p101-cmd",
    "run": "p101-run",
    "protect_tripped": "p101-protect-tripped",
    "other_cmd": "p102-cmd",
    "other_run": "p102-run",
    "lal_ack": "lal-ack",
    "lal_alarm": "lal-alarm",
    "lal_unack": "lal-unacknowledged",
}

# The held levels, in the emitted model's engineering units: the
# clamp's low side (inside `cutoff`), inside the low-level alarm's
# declared hysteresis band (past `cutoff`, below `low_limit +
# hysteresis`), and past the band. Resolved against the emitted
# chain's declared `cutoff`/`stop`/`start` at runtime — these names
# only fix the walk's order.
HELD_CLAMP = 1.4
HELD_BAND = 1.55
HELD_CLEAR = 1.65


def signal_points(model):
    """The `{key: point id}` map the emitted model's signal index
    declares for the leg's cutoff seam — None when the model declares
    no such seam, the writable points the receipted writes need
    checked against the declared `io_points` writable set."""
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
    for key in ("mode", "hand", "lal_ack"):
        if points[key] not in writable:
            return None
    return points


def chain_setpoints(model):
    """The emitted `threshold-chain`'s declared `cutoff`/`start`
    setpoints beside the managed `lal`'s `low_limit`/`hysteresis` —
    the leg's clamp, band, and resume bounds read out of the model,
    never assumed. Returns `(cutoff, start, low_limit, hysteresis)`."""
    cutoff = start = None
    for component in model.get("components", []):
        if component.get("kind") != "threshold-chain":
            continue
        parameters = component.get("parameters", {})
        cutoff = parameters.get("cutoff", {}).get("float")
        start = parameters.get("start", {}).get("float")
    # The managed low-level alarm: the `latching`-kind component whose
    # declared low limit stands nearest the chain's `cutoff` — the
    # station's `lal`, distinguished from the high-level alarm's
    # never-tight floor.
    low_limit = hysteresis = None
    if cutoff is not None:
        for component in model.get("components", []):
            if "latching" not in component.get("kind", ""):
                continue
            parameters = component.get("parameters", {})
            limit = parameters.get("low_limit", {}).get("float")
            if limit is not None and limit <= cutoff and (
                low_limit is None or limit > low_limit
            ):
                low_limit = limit
                hysteresis = parameters.get("hysteresis", {}).get("float")
    if None in (cutoff, start, low_limit, hysteresis):
        return None
    return cutoff, start, low_limit, hysteresis


def value(snapshot, point):
    """The point's latest sample value — `simulate.snapshot_point`."""
    return simulate.snapshot_point(snapshot, point)


def flag(snapshot, point):
    """The point's latest Bool sample value, or None."""
    reading = value(snapshot, point)
    return reading.get("bool") if isinstance(reading, dict) else None


def owner_token(preamble):
    """The field-ownership token a launched active's claim line
    reports — `field write-ownership claim held under owner token N`
    ahead of the listener where the release records it; None where
    the release line claims only on promotion."""
    for line in preamble:
        claimed = re.search(r"owner token (\d+)", line)
        if claimed:
            return int(claimed.group(1))
    return None


def join_writer(plant_io, token, failures):
    """Join the field owner's writer claim — `ensure_writer` under
    the token the launch reported — so the leg's held level writes
    land inside the standing claim rather than fencing it."""
    if token is None:
        return
    verdict = plant_io.request({"op": "ensure_writer", "owner": token})
    if verdict.get("result") not in ("done", "claimed_shared"):
        failures.append(
            f"the writer-claim join under the recorded owner token "
            f"answered {verdict}"
        )
        raise Abort


def hold_level(plant_io, points, level, failures):
    """One held-value write on each declared level field channel —
    the plant protocol's `write` stamps the channel until the field's
    next step re-scans it, so the leg re-issues the hold before every
    driven tick: the served level the pair's scans read, pinned at
    `level` for exactly that scan."""
    for point in (points["level_primary"], points["level_backup"]):
        verdict = plant_io.request(
            {"op": "write", "point": point, "value": {"float": level}}
        )
        if verdict.get("result") != "done":
            failures.append(
                f"the held level write on point {point} answered {verdict}"
            )
            raise Abort


def submit(url, command, failures):
    """POST one receipted command to the active's `/command` and assert
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


def tick(rig, failures):
    """One driven pair tick — the harness's tracking-first scan,
    identical images asserted — returns the owner's served
    snapshot."""
    return rig.tick(rig.standby_url, rig.duty_url, failures)[1]


def drive_until(rig, failures, condition, bound=SETTLE_BOUND, hold=None):
    """Driven pair ticks until `condition(owner_snapshot)` holds —
    returns the satisfying snapshot, or None when `bound` scans pass
    without it landing. `hold`, when given, runs before each tick —
    the leg's held level writes re-issued between the field's steps."""
    for _ in range(bound):
        if hold is not None:
            hold()
        owner = tick(rig, failures)
        if condition(owner):
            return owner
    return None


def cutoff_pass(args, tamper):
    """The cutoff run: converge, demand, manual draw-down, clamp,
    alarm lifecycle, recovery, restore, audit. Returns
    `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the cutoff leg "
            "has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    with open(args.model) as handle:
        model = json.load(handle)
    points = signal_points(model)
    if points is None:
        raise Abort(
            "the emitted model declares no writable per-pump mode seam "
            "or low-level alarm — the cutoff leg has nothing to exercise"
        )
    setpoints = chain_setpoints(model)
    if setpoints is None:
        raise Abort(
            "the emitted model declares no threshold-chain cutoff/start "
            "setpoints or managed low-level alarm — the cutoff leg has "
            "nothing to exercise"
        )
    cutoff_level, start_level, low_limit, hysteresis = setpoints
    band_top = low_limit + hysteresis
    if not (
        HELD_CLAMP <= cutoff_level
        and cutoff_level < HELD_BAND <= band_top
        and band_top <= HELD_CLEAR < start_level
    ):
        raise Abort(
            f"the emitted setpoints cutoff {cutoff_level}, start "
            f"{start_level}, low_limit {low_limit}, hysteresis "
            f"{hysteresis} leave the leg's held walk "
            f"({HELD_CLAMP}/{HELD_BAND}/{HELD_CLEAR}) unordered — the "
            "walk cannot exercise the declared lifecycle"
        )

    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared, tamper)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        plant_io = rig.plant_io
        token = owner_token(rig.duty_preamble)
        join_writer(plant_io, token, failures)

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

        # Phase 2 — the duty demand: the simulated well fills under the
        # declared inflow until the pump group holds a duty demand — a
        # running duty pump the cutoff must stop.
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: value(snapshot, points["demand"]).get("int", 0)
            >= 1
            and flag(snapshot, points["cmd"]) is True
            and flag(snapshot, points["run"]) is True,
            bound=DEMAND_BOUND,
        )
        if owner is None:
            failures.append(
                "the pump group never held a duty demand — the cutoff "
                "leg's precondition never arrived"
            )
            raise Abort
        evidence["demand_at"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "demand",
                "tick": owner["tick"],
                "demand": value(owner, points["demand"]),
                "duty": value(owner, points["duty"]),
                "staged": value(owner, points["staged"]),
            }
        )

        # Phase 3 — the manual draw-down: receipted `mode`/`hand`
        # writes put the pump on the operator demand so the well draws
        # past the declared `stop` crossing — the chain's demand
        # releasing while the hand-run machine keeps pulling — down to
        # the declared `cutoff` where the protection interlock trips.
        mode_write = takeover.write_value(points["mode"], True)
        mode_receipt = submit(duty_url, mode_write, failures)
        hand_write = takeover.write_value(points["hand"], True)
        hand_receipt = submit(duty_url, hand_write, failures)
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: flag(snapshot, points["cmd"]) is True
            and flag(snapshot, points["run"]) is True,
        )
        if owner is None:
            failures.append(
                "the receipted hand write never ran the pump — the "
                "delivered command or the run feedback never asserted "
                "on the operator demand"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "manual",
                "mode_receipt": mode_receipt,
                "hand_receipt": hand_receipt,
                "running": {
                    "cmd": value(owner, points["cmd"]),
                    "run": value(owner, points["run"]),
                },
            }
        )

        # Phase 4 — the clamp: the held operator demand draws the well
        # to the declared cutoff. `below_cutoff` rises, `demand` reads
        # 0 with `duty_call`/`lag_call` off, every pump call is off —
        # the running pump's proven run releasing on the trip — and
        # the managed `lal` annunciates `alarm`/`unacknowledged`.
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: flag(snapshot, points["below_cutoff"]) is True
            and flag(snapshot, points["protect_tripped"]) is True
            and flag(snapshot, points["cmd"]) is False,
            bound=DRAIN_BOUND,
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                f"the held operator demand never reached the low-level "
                f"cutoff — below-cutoff reads "
                f"{value(owner, points['below_cutoff'])}, the "
                f"protection trip "
                f"{value(owner, points['protect_tripped'])}, the "
                f"delivered command {value(owner, points['cmd'])}"
            )
            raise Abort
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: flag(snapshot, points["run"]) is False
            and flag(snapshot, points["other_run"]) is False,
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                f"the running pump's proven run never released on the "
                f"cutoff trip — run reads "
                f"{value(owner, points['run'])}, the other pump "
                f"{value(owner, points['other_run'])}"
            )
            raise Abort
        clamp = {
            "tick": owner["tick"],
            "below_cutoff": value(owner, points["below_cutoff"]),
            "demand": value(owner, points["demand"]),
            "duty_call": value(owner, points["duty_call"]),
            "lag_call": value(owner, points["lag_call"]),
            "cmd": value(owner, points["cmd"]),
            "other_cmd": value(owner, points["other_cmd"]),
            "run": value(owner, points["run"]),
            "other_run": value(owner, points["other_run"]),
            "lal_alarm": value(owner, points["lal_alarm"]),
            "lal_unack": value(owner, points["lal_unack"]),
        }
        if tamper == "demand-held":
            if clamp["demand"].get("int", 0) < 1:
                failures.append(
                    f"demand reads {clamp['demand']} at below_cutoff — "
                    "the doctored leg expected demand to stay asserted"
                )
        elif clamp["demand"] != {"int": 0}:
            failures.append(
                f"demand reads {clamp['demand']} at below_cutoff, "
                "expected 0 — the declared clamp releases every call"
            )
        for key, want in (("duty_call", False), ("lag_call", False),
                          ("cmd", False), ("other_cmd", False),
                          ("run", False), ("other_run", False)):
            if clamp[key] != {"bool": want}:
                failures.append(
                    f"{key} reads {clamp[key]} at below_cutoff, "
                    f"expected {want} — every pump call must release"
                )
        if tamper == "lal-silent":
            if clamp["lal_alarm"] == {"bool": True} or clamp[
                "lal_unack"
            ] == {"bool": True}:
                failures.append(
                    f"the managed lal annunciated — alarm reads "
                    f"{clamp['lal_alarm']}, unacknowledged "
                    f"{clamp['lal_unack']} — the doctored leg expected "
                    "it never to report"
                )
        else:
            if clamp["lal_alarm"] != {"bool": True} or clamp[
                "lal_unack"
            ] != {"bool": True}:
                failures.append(
                    f"the managed lal never annunciated the cutoff — "
                    f"alarm reads {clamp['lal_alarm']}, unacknowledged "
                    f"{clamp['lal_unack']}"
                )
        if failures:
            raise Abort
        evidence["clamped_at"] = owner["tick"]
        digest_entries.append({"phase": "clamp", **clamp})

        # Phase 5 — the held level and the receipted ack. The alarm's
        # `alarm` output follows the live condition, so the leg pins
        # the served level inside the clamp — one held write per
        # driven tick through the shared writer claim — while the
        # `lal-ack` write settles: `unacknowledged` must clear with
        # `alarm` still standing.
        hold = lambda: hold_level(plant_io, points, HELD_CLAMP, failures)
        ack_write = takeover.write_value(points["lal_ack"], True)
        ack_receipt = submit(duty_url, ack_write, failures)
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: flag(snapshot, points["lal_unack"]) is False,
            hold=hold,
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                f"the receipted lal ack never cleared the latch — "
                f"unacknowledged reads "
                f"{value(owner, points['lal_unack'])}"
            )
            raise Abort
        if flag(owner, points["lal_alarm"]) is not True:
            failures.append(
                f"the lal latch cleared with the alarm already "
                f"returned — alarm reads "
                f"{value(owner, points['lal_alarm'])} while the ack "
                "settled: the receipted ack must clear unacknowledged "
                "while alarm stands"
            )
            raise Abort
        ack_release = takeover.write_value(points["lal_ack"], False)
        submit(duty_url, ack_release, failures)
        # One held tick lets the release write settle while the clamp
        # still stands — the settlement journaled before the walk's
        # `below_cutoff` return.
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: True,
            bound=1,
            hold=hold,
        )
        digest_entries.append(
            {
                "phase": "ack",
                "receipt": ack_receipt,
                "alarm": value(owner, points["lal_alarm"]),
                "unacknowledged": value(owner, points["lal_unack"]),
            }
        )

        # Phase 6 — the hysteresis walk: the held level crosses the
        # clamp boundary into the alarm's declared band — `alarm`
        # standing while `below_cutoff` releases — then past the band
        # where the alarm returns per its declared lifecycle.
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: flag(snapshot, points["below_cutoff"]) is False
            and flag(snapshot, points["lal_alarm"]) is True,
            bound=SETTLE_BOUND,
            hold=lambda: hold_level(plant_io, points, HELD_BAND, failures),
        )
        if owner is None:
            failures.append(
                "the held walk never stood the alarm inside its "
                "declared hysteresis band — below_cutoff or alarm "
                "did not report the band crossing"
            )
            raise Abort
        band = {
            "below_cutoff": value(owner, points["below_cutoff"]),
            "alarm": value(owner, points["lal_alarm"]),
            "unacknowledged": value(owner, points["lal_unack"]),
        }
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: flag(snapshot, points["lal_alarm"]) is False,
            bound=SETTLE_BOUND,
            hold=lambda: hold_level(plant_io, points, HELD_CLEAR, failures),
        )
        if owner is None:
            failures.append(
                "the managed lal never returned past its declared "
                "hysteresis — alarm held with the level clear"
            )
            raise Abort
        evidence["returned_at"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "hysteresis",
                "band": band,
                "returned": value(owner, points["lal_alarm"]),
            }
        )

        # Phase 7 — the restore and the resume: the driven inputs
        # release — the held writes stop, `mode`/`hand` return the
        # pump to group control — and the field's own level rises to
        # `start`, where `demand` resumes and a pump call stands.
        restored = []
        for key in ("mode", "hand"):
            command = takeover.write_value(points[key], False)
            submit(duty_url, command, failures)
            restored.append(command)
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: value(snapshot, points["demand"]).get("int", 0)
            >= 1
            and flag(snapshot, points["below_cutoff"]) is False
            and (
                flag(snapshot, points["cmd"]) is True
                or flag(snapshot, points["other_cmd"]) is True
            ),
            bound=RESUME_BOUND,
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                f"demand never resumed once the level cleared the "
                f"clamp — demand reads "
                f"{value(owner, points['demand'])}, below_cutoff "
                f"{value(owner, points['below_cutoff'])}, the pump "
                f"calls {value(owner, points['cmd'])} / "
                f"{value(owner, points['other_cmd'])}"
            )
            raise Abort
        receipts_duty = pair.get(
            f"{duty_url}/receipts", "GET /receipts", failures
        )
        for command in restored:
            if not takeover.settled(receipts_duty, command):
                failures.append(
                    f"the restore write {command} never settled "
                    "applied into the adopted receipt log"
                )
        if not takeover.settled(receipts_duty, mode_write) or not (
            takeover.settled(receipts_duty, hand_write)
        ):
            failures.append(
                "the takeover writes never settled applied into the "
                "adopted receipt log"
            )
        if failures:
            raise Abort
        evidence["resumed_at"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "resume",
                "tick": owner["tick"],
                "demand": value(owner, points["demand"]),
                "duty_call": value(owner, points["duty_call"]),
                "cmd": value(owner, points["cmd"]),
                "other_cmd": value(owner, points["other_cmd"]),
            }
        )

        # Phase 8 — the audit: the pair's roles never moved — a level
        # excursion is a plant event, not a failover — and the
        # active's served journal carries the leg's attributed
        # transitions in run order.
        standby_role = pair.get(f"{standby_url}/role", "GET /role", failures)
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        sync = standby_role.get("sync")
        if standby_role.get("role") != "standby" or not (
            isinstance(sync, dict) and "tracking" in sync
        ):
            failures.append(
                f"the tracking peer's role moved — GET /role answers "
                f"{standby_role} after the cutoff run"
            )
        if duty_role.get("role") != "active":
            failures.append(
                f"the field owner reports {duty_role.get('role')!r} "
                "after the cutoff run, expected active"
            )
        journal = pair.get(f"{duty_url}/journal", "GET /journal", failures)
        groups = [
            # The manual selection: both receipted writes and the mode
            # point's journaled transition.
            [
                ("settled", points["mode"], {"bool": True}, "applied", ACTOR),
                ("settled", points["hand"], {"bool": True}, "applied", ACTOR),
                ("changed", points["mode"], {"bool": True}),
            ],
            # The clamp: below_cutoff rises, the managed lal
            # annunciates, and the running pump's proven feedback
            # releases.
            [("changed", points["below_cutoff"], {"bool": True})],
            [("changed", points["lal_alarm"], {"bool": True})],
            [("changed", points["lal_unack"], {"bool": True})],
            [("changed", points["protect_tripped"], {"bool": True})],
            [("changed", points["run"], {"bool": False})],
            # The receipted ack clearing the latch while alarm stands.
            [
                ("settled", points["lal_ack"], {"bool": True}, "applied", ACTOR),
                ("changed", points["lal_unack"], {"bool": False}),
                ("settled", points["lal_ack"], {"bool": False}, "applied", ACTOR),
            ],
            # The hysteresis return: below_cutoff releasing inside the
            # band, the alarm returning past it.
            [
                ("changed", points["below_cutoff"], {"bool": False}),
                ("changed", points["lal_alarm"], {"bool": False}),
            ],
            # The restore: the writes settling the pump back to group
            # control.
            [
                ("settled", points["mode"], {"bool": False}, "applied", ACTOR),
                ("settled", points["hand"], {"bool": False}, "applied", ACTOR),
                ("changed", points["mode"], {"bool": False}),
            ],
        ]
        failures.extend(takeover.ordered_group_misses(journal, groups))
        if failures:
            raise Abort
        digest_entries.append(
            {"phase": "audit", "events": takeover.journal_events(journal)}
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
        choices=["demand-held", "lal-silent"],
        help="doctor the leg's expectations — the pass must fail "
        "naming the evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = cutoff_pass(args, args.tamper)
    except Abort as abort:
        for line in abort.args:
            eprint(f"cutoff: {line}")
        return 1
    for failure in failures:
        eprint(f"cutoff: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"cutoff: the {args.tamper} case passed silently — the "
                "leg never noticed the doctored expectation"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"cutoff-digest {digest} — tracking by tick "
        f"{evidence['converged']}, duty demand at tick "
        f"{evidence['demand_at']}, clamped at tick "
        f"{evidence['clamped_at']}, alarm returned at tick "
        f"{evidence['returned_at']}, demand resumed at tick "
        f"{evidence['resumed_at']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
