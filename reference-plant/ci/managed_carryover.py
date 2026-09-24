#!/usr/bin/env python3
"""The pair contract's managed run-state carryover leg — the
consumer-side proof that the managed alarm kinds' checkpointed run
state carries across a promotion on the deployed redundant pair
(WW-ENG-003, WW-ALM-002, WW-LCM-001).

ci/check.sh runs this script twice against the same declared
deployment as ci/legs/pair.py — `dcs-plant-server` serving the emitted
model and dynamics, plus the two manifest-declared `dcs-controller
--driven --remote` peers, the standby wired at the field owner's
monitor. The managed-lifecycle leg proves the managed surface's
behaviors on a settled pair; this leg proves the run-state machine
itself — the mid-shelve countdown and the out-of-service level the
kinds checkpoint — carries across a takeover: a promotion that
restarted the shelve bound or dropped the operator's inhibit is
exactly the misbehavior the continuity clause forbids. With the pair
converged and tracking, the leg:

- puts the per-pump fault alarm out of service through its wired
  `oos` point — the receipted `write_value` settling `applied`, the
  declared `out_of_service`/`suppress` wiring reporting both managed
  states — then drives a run-contact fault through the plant
  protocol's unfenced surface so `alarm` reports the process truth
  while suppression holds the `unacknowledged` latch: the standing
  suppressed trip the carry must keep held;
- shelves the declared shelvable `lal` alarm through its writable
  journaled `shelve` point mid-run — `shelved` asserting, the
  checkpointed countdown running toward the emitted model's declared
  `max_shelve_ticks` expiry;
- issues the documented switch — `POST /demote` on the field owner,
  `POST /promote` on the converged standby — inside the shelve
  bound, while the flag still stands;
- asserts on the promoted peer that `shelved` still stands and
  releases at the tick the countdown would have expired — the
  journaled assertion-to-expiry span measuring the declared bound
  with the promotion inside it, the checkpointed timer continued
  rather than restarted — that `out_of_service` still stands with
  evaluation held: `out_of_service`/`suppressed` reporting, `alarm`
  still reporting the standing truth, the `unacknowledged` latch
  still withheld; and that every written point rode the checkpoint —
  the shelve request and the `oos` level reading as written;
- restores every written point and driven input on the promoted
  peer — the cleared contact, the returned-to-service inhibit, the
  released shelve request — then the pair's launch roles, leaving
  the manifest-declared duty `active` and its standby `tracking`;
- audits both peers' durable journal files: each carrying its
  single cold-start boundary with `seq` order 1..n intact, the
  promoted peer's record ordering the adopted settlements, the
  promotion's `role_changed` entries, the countdown's expiry at the
  asserted tick, and the restore's settlements in run order — the
  ordered record continuous across the switch, the served journal
  answering the same record.

Usage:

    managed_carryover.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `carry-digest <sha256>` line prints — the check runs
two passes and compares them (`carry-nondeterministic`). A contract
violation reports `carry: …` lines on stderr and exits 1 — the
check's `carry-failed`. `--tamper` doctors the leg's own expectation
so a doctored implementation — a promoted peer restarting the shelve
bound or dropping `out_of_service` — would pass while the honest run
reports the named failure:

- `restarted-bound` asserts the release lands at the bound counted
  fresh from the switch — the honest earlier expiry fails it;
- `dropped-oos` asserts the promoted peer reports neither
  `out_of_service` nor `suppressed` — the honest standing states
  fail it.

Both exit 1 like any failure: the check asserts each reports the
named diagnostic rather than passing silently.
"""

import argparse
import hashlib
import json
import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "legs")
)

import managed_lifecycle
import pair
import simulate


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The bound each driven phase gets to land its declared effect across
# the wiring's one-scan carrier crossings. The actor the leg's
# receipted submissions declare — the attribution every settled
# receipt must carry.
SETTLE_BOUND = 16
RESTORE_TICKS = 4
ACTOR = "ci-carry"

# The injected non-Good the fault surface carries — the same quality
# the lifecycle leg's suppressed trip and the takeover leg's
# protection drive use.
BAD_QUALITY = {"bad": "device_fault"}

# The signal names resolving the leg's point ids out of the emitted
# model — the same names the signal index serves, so the leg exercises
# the declared seam, never a hard-coded id.
SIGNALS = {
    "lal_shelve": "lal-shelve",
    "lal_shelved": "lal-shelved",
    "pump_oos": "p101-oos",
    "run": "p101-run",
    "fault": "p101-fault",
    "fault_alarm": "p101-fault-alarm",
    "fault_unack": "p101-fault-unacknowledged",
    "fault_suppressed": "p101-fault-suppressed",
    "fault_oos": "p101-fault-out-of-service",
}

# The receipted seams the leg commands — each must be a declared
# writable journaled `In` point: the shelve request and the
# out-of-service level are the run state the carry rides on, and the
# durable record the audit reads journals their transitions.
WRITABLE_JOURNALED = ("lal_shelve", "pump_oos")


def signal_points(model):
    """The `{key: point id}` map the emitted model's signal index
    declares for the leg's carry — None when the model declares no
    such surface. A signal's `source` is the point it names; the
    lowest-signal-id-wins rule the served index applies. The
    receipted seams must be declared writable *and* journaled `In`
    points — the writable flag admits the receipted write, the
    journaled flag puts the transition on the durable record the
    audit reads."""
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
    declared = {
        point["id"]: point for point in model.get("io_points", [])
    }
    for key in WRITABLE_JOURNALED:
        point = declared.get(points[key], {})
        if not point.get("writable") or not point.get("journaled"):
            return None
    return points


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


def settled_receipt(receipts, command):
    """The adopted receipt log's entry for `command` settled applied,
    or None."""
    return next(
        (
            entry
            for entry in receipts
            if entry.get("command") == command
            and simulate.receipt_outcome(entry) == "applied"
        ),
        None,
    )


def settled(receipts, command):
    """True when the adopted receipt log carries `command` settled
    applied."""
    return settled_receipt(receipts, command) is not None


def drive_until(rig, tracked_url, owner_url, failures, condition, bound=SETTLE_BOUND):
    """Driven pair ticks — the tracking peer scanned first so its
    pull applies the owner's latest checkpoint — until
    `condition(owner_snapshot)` holds. Returns the satisfying
    snapshot, or None when `bound` scans pass without it landing."""
    for _ in range(bound):
        _tracked, owner = rig.tick(tracked_url, owner_url, failures)
        if condition(owner):
            return owner
    return None


def journal_events(entries):
    """The leg's audit stream out of a journal entry list —
    `("settled", point, value, outcome, actor)` for each command
    receipt, `("changed", point, to)` for each journaled value
    transition, `("quality", point, to)` for each quality transition,
    and `("role", from, to)` for each reported-role transition — in
    `seq` order."""
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
        elif "role_changed" in event:
            change = event["role_changed"]
            events.append(("role", change.get("from"), change.get("to")))
    return events


def journal_entries(files, name, failures):
    """One peer's durable journal entries — the `--journal-file`'s
    `entry` records — after asserting the file exists, carries its
    single cold-start boundary, and keeps `seq` order 1..n: the
    ordered-record continuity the leg audits across the switch."""
    journal_path = files.get("journal_file")
    if journal_path is None or not os.path.exists(journal_path):
        failures.append(
            f"{name}'s declared journal file does not exist — the "
            "--journal-file flag was not honored"
        )
        raise Abort
    records = pair.journal_records(journal_path)
    boundaries = [record for kind, record in records if kind == "boundary"]
    if boundaries != [{"run": 1, "tick": 0}]:
        failures.append(
            f"{name}'s journal boundaries are {boundaries} — the "
            "record did not stay continuous across the switch, "
            "expected the single cold-start marker"
        )
        raise Abort
    entries = [record for kind, record in records if kind == "entry"]
    seqs = [entry["seq"] for entry in entries]
    if seqs != list(range(1, len(seqs) + 1)):
        failures.append(
            f"{name}'s journal seqs are not 1..n in order: {seqs} — "
            "the ordered record broke across the switch"
        )
        raise Abort
    return entries


def carryover_pass(args, tamper):
    """The carryover run: converge, inhibit, trip, shelve, switch,
    carry, release, restore, audit. Returns `(digest_entries,
    evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the carryover "
            "leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    with open(args.model) as handle:
        model = json.load(handle)
    points = signal_points(model)
    if points is None:
        raise Abort(
            "the emitted model declares no managed-alarm carryover "
            "seam — the leg has nothing to exercise"
        )
    bound = managed_lifecycle.shelve_bound(model, points["lal_shelve"])
    if bound is None or bound < 3:
        raise Abort(
            "no component's shelve port binds the declared lal shelve "
            "point inside a usable bound — the model's "
            "max_shelve_ticks is unresolvable"
        )
    true, false = {"bool": True}, {"bool": False}
    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        plant_io = rig.plant_io

        # Phase 1 — convergence: the pair leg's driven-tick loop, the
        # tracking peer scanned first so each pull applies the owner's
        # latest checkpoint and the peers rest at the same tick with
        # identical images.
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
        owner = converged["owner"]
        for key in (
            "lal_shelved",
            "fault_oos",
            "fault_suppressed",
            "fault_unack",
            "lal_shelve",
            "pump_oos",
        ):
            if value(owner, points[key]) != false:
                failures.append(
                    f"the converged pair is not quiet — {key} reads "
                    f"{value(owner, points[key])} before the leg drives"
                )
        if failures:
            raise Abort

        # Phase 2 — the designed suppression: the pump's `oos` point
        # takes the receipted inhibit; the fault alarm's declared
        # `oos`/`suppress` wiring reports `out_of_service` and
        # `suppressed` — the delivered `suppress` copy landing a
        # carrier crossing after the direct `oos`.
        oos_write = write_value(points["pump_oos"], True)
        submit(duty_url, oos_write, failures)
        owner = drive_until(
            rig,
            standby_url,
            duty_url,
            failures,
            lambda snapshot: value(snapshot, points["fault_oos"]) == true
            and value(snapshot, points["fault_suppressed"]) == true,
        )
        if owner is None:
            owner = rig.tick(standby_url, duty_url, failures)[1]
            failures.append(
                "the maintenance inhibit never reported — "
                f"out_of_service reads "
                f"{value(owner, points['fault_oos'])}, suppressed "
                f"{value(owner, points['fault_suppressed'])}"
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
        evidence["oos_at"] = owner["tick"]

        # Phase 3 — the held trip: the injected non-Good on the run
        # contact defeats the feedback's proof, `fault` asserts the
        # alarm's `in`, and `alarm` reports the standing truth while
        # suppression withholds the latch — the evaluation-held state
        # the carry must keep held.
        verdict = plant_io.request(
            {
                "op": "inject_fault",
                "point": points["run"],
                "fault": {"quality": BAD_QUALITY},
            }
        )
        if verdict.get("result") != "done":
            failures.append(f"inject_fault on the run contact answered {verdict}")
            raise Abort
        owner = drive_until(
            rig,
            standby_url,
            duty_url,
            failures,
            lambda snapshot: value(snapshot, points["fault"]) == true
            and value(snapshot, points["fault_alarm"]) == true
            and value(snapshot, points["fault_unack"]) == false
            and value(snapshot, points["fault_suppressed"]) == true,
        )
        if owner is None:
            owner = rig.tick(standby_url, duty_url, failures)[1]
            failures.append(
                "the suppressed trip never reported — fault reads "
                f"{value(owner, points['fault'])}, alarm "
                f"{value(owner, points['fault_alarm'])}, "
                f"unacknowledged "
                f"{value(owner, points['fault_unack'])}, suppressed "
                f"{value(owner, points['fault_suppressed'])}"
            )
            raise Abort
        evidence["tripped_at"] = owner["tick"]

        # Phase 4 — the mid-run shelve: the shelvable alarm's writable
        # journaled `shelve` point takes the receipted request and
        # `shelved` reports — the checkpointed countdown starting its
        # run toward the declared `max_shelve_ticks` expiry.
        shelve_write = write_value(points["lal_shelve"], True)
        submit(duty_url, shelve_write, failures)
        owner = drive_until(
            rig,
            standby_url,
            duty_url,
            failures,
            lambda snapshot: value(snapshot, points["lal_shelved"]) == true,
        )
        if owner is None:
            owner = rig.tick(standby_url, duty_url, failures)[1]
            failures.append(
                "the receipted shelve never reported shelved — "
                f"shelved reads "
                f"{value(owner, points['lal_shelved'])}, the request "
                f"point {value(owner, points['lal_shelve'])}"
            )
            raise Abort
        receipt = settled_receipt(
            pair.get(f"{duty_url}/receipts", "GET /receipts", failures),
            shelve_write,
        )
        if receipt is None:
            failures.append(
                "the shelve write never settled applied into the "
                "adopted receipt log"
            )
            raise Abort
        # The applying scan counts as the countdown's first shelved
        # scan — the receipt's applied tick is the assertion the
        # declared bound measures from, so the continued timer expires
        # `max_shelve_ticks` scans later.
        shelved_at = receipt["outcome"]["applied"]["tick"]
        evidence["shelved_at"] = shelved_at
        expect_release = shelved_at + bound
        # A mid-shelve pause — the countdown genuinely in flight when
        # the switch lands — while the bound keeps room for the
        # promotion and the carried release.
        owner = rig.tick(standby_url, duty_url, failures)[1]
        if value(owner, points["lal_shelved"]) != true:
            failures.append(
                "the standing shelve dropped inside its own bound "
                f"before the switch — shelved reads "
                f"{value(owner, points['lal_shelved'])} at tick "
                f"{owner['tick']}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "managed",
                "bound": bound,
                "oos_at": evidence["oos_at"],
                "tripped_at": evidence["tripped_at"],
                "shelved_at": shelved_at,
            }
        )

        # Phase 5 — the switch inside the bound: demote the field
        # owner, promote the converged standby, each answered by its
        # RoleReport — the countdown in flight across it.
        switch_tick = owner["tick"]
        demote = rig.demote(duty_url, failures)
        promote = rig.promote(standby_url, failures)
        evidence["switched_at"] = demote["tick"]
        if tamper == "restarted-bound":
            # The doctored expectation: a promoted peer that restarted
            # the countdown at the switch would count its first
            # post-promotion scan as the bound's first — expiry a
            # fresh `max_shelve_ticks` later than the honest
            # continued run's.
            expect_release = switch_tick + bound + 1
        digest_entries.append(
            {"phase": "switch", "demote": demote, "promote": promote}
        )

        # Phase 6 — the carry: the promoted peer scanned after the
        # demoted tracker's pull each tick, the peers' images
        # identical. Every tick the managed states must stand carried:
        # `shelved` while the countdown runs — releasing at the tick
        # the continued timer expires, never the restarted bound's —
        # `out_of_service`/`suppressed` reporting, `alarm` the
        # standing truth, and the `unacknowledged` latch withheld.
        carried_ticks = []
        released_tick = None
        for _ in range(bound + SETTLE_BOUND):
            tracked, owner = pair.tick(duty_url, standby_url, failures)
            if tamper == "dropped-oos":
                if value(owner, points["fault_oos"]) != false or value(
                    owner, points["fault_suppressed"]
                ) != false:
                    failures.append(
                        "the promoted peer still reports "
                        f"out_of_service "
                        f"{value(owner, points['fault_oos'])}, "
                        f"suppressed "
                        f"{value(owner, points['fault_suppressed'])} "
                        f"at tick {owner['tick']} — expected the "
                        "dropped carry"
                    )
                    raise Abort
            else:
                if (
                    value(owner, points["fault_oos"]) != true
                    or value(owner, points["fault_suppressed"]) != true
                ):
                    failures.append(
                        "the promoted peer dropped the maintenance "
                        f"inhibit — out_of_service reads "
                        f"{value(owner, points['fault_oos'])}, "
                        f"suppressed "
                        f"{value(owner, points['fault_suppressed'])} "
                        f"at tick {owner['tick']}"
                    )
                    raise Abort
                if (
                    value(owner, points["fault"]) != true
                    or value(owner, points["fault_alarm"]) != true
                    or value(owner, points["fault_unack"]) != false
                ):
                    failures.append(
                        "the carried evaluation does not hold — fault "
                        f"reads {value(owner, points['fault'])}, alarm "
                        f"{value(owner, points['fault_alarm'])}, "
                        f"unacknowledged "
                        f"{value(owner, points['fault_unack'])} at "
                        f"tick {owner['tick']}"
                    )
                    raise Abort
            if value(owner, points["lal_shelve"]) != true:
                failures.append(
                    "the shelve request did not ride the promotion's "
                    f"adopted state — the request point reads "
                    f"{value(owner, points['lal_shelve'])} at tick "
                    f"{owner['tick']}"
                )
                raise Abort
            if value(owner, points["pump_oos"]) != true:
                failures.append(
                    "the out-of-service level did not ride the "
                    f"promotion's adopted state — the oos point reads "
                    f"{value(owner, points['pump_oos'])} at tick "
                    f"{owner['tick']}"
                )
                raise Abort
            shelved = value(owner, points["lal_shelved"])
            carried_ticks.append((owner["tick"], shelved))
            if shelved == false:
                released_tick = owner["tick"]
                break
        if released_tick is None:
            failures.append(
                f"the carried shelve never released — shelved still "
                f"reads true after ticks {carried_ticks}, the "
                f"declared bound {bound}"
            )
            raise Abort
        if released_tick != expect_release:
            if tamper == "restarted-bound":
                failures.append(
                    f"the mid-shelve countdown released at tick "
                    f"{released_tick}, expected the restarted bound's "
                    f"expiry at tick {expect_release} — the "
                    "checkpointed timer continued rather than "
                    "restarting"
                )
            else:
                failures.append(
                    f"the mid-shelve countdown released at tick "
                    f"{released_tick}, expected the continued bound's "
                    f"expiry at tick {expect_release} — the "
                    "checkpointed timer did not carry identically"
                )
            raise Abort
        evidence["released_at"] = released_tick

        # The standing flag stays released while the request stands —
        # the bound's own expiry, never a re-arm — and the peers
        # settle into their new roles.
        for _ in range(2):
            tracked, owner = pair.tick(duty_url, standby_url, failures)
            if (
                value(owner, points["lal_shelved"]) != false
                or value(owner, points["lal_shelve"]) != true
            ):
                failures.append(
                    "the expired shelve re-armed or the request "
                    f"dropped — shelved reads "
                    f"{value(owner, points['lal_shelved'])}, the "
                    f"request point "
                    f"{value(owner, points['lal_shelve'])} at tick "
                    f"{owner['tick']}"
                )
                raise Abort
            carried_ticks.append((owner["tick"], false))
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        standby_role = pair.get(f"{standby_url}/role", "GET /role", failures)
        if standby_role.get("role") != "active":
            failures.append(
                f"the promoted peer reports {standby_role.get('role')!r}, "
                "expected active"
            )
            raise Abort
        sync = duty_role.get("sync")
        if duty_role.get("role") != "standby" or not (
            isinstance(sync, dict) and "tracking" in sync
        ):
            failures.append(
                "the demoted peer never reconverged — GET /role "
                f"answers {duty_role}"
            )
            raise Abort
        evidence["carried_through"] = carried_ticks[-1][0]
        digest_entries.append(
            {
                "phase": "carry",
                "ticks": carried_ticks,
                "released_at": released_tick,
                "expect_release": expect_release,
                "duty_role": duty_role,
                "standby_role": standby_role,
            }
        )

        # Phase 7 — the restores on the promoted peer: the cleared
        # contact returns the standing trip — the latch never stood,
        # so no ack is owed — the receipted return to service drops
        # `out_of_service`/`suppressed` without a fresh trip, and the
        # released shelve request withdraws.
        verdict = plant_io.request(
            {"op": "clear_fault", "point": points["run"]}
        )
        if verdict.get("result") != "done":
            failures.append(f"clear_fault on the run contact answered {verdict}")
            raise Abort
        owner = drive_until(
            rig,
            duty_url,
            standby_url,
            failures,
            lambda snapshot: value(snapshot, points["fault"]) == false
            and value(snapshot, points["fault_alarm"]) == false
            and value(snapshot, points["fault_unack"]) == false,
        )
        if owner is None:
            owner = pair.tick(duty_url, standby_url, failures)[1]
            failures.append(
                "the cleared contact left the fault standing — fault "
                f"reads {value(owner, points['fault'])}, alarm "
                f"{value(owner, points['fault_alarm'])}, "
                f"unacknowledged "
                f"{value(owner, points['fault_unack'])}"
            )
            raise Abort
        service_write = write_value(points["pump_oos"], False)
        submit(standby_url, service_write, failures)
        owner = drive_until(
            rig,
            duty_url,
            standby_url,
            failures,
            lambda snapshot: value(snapshot, points["fault_oos"]) == false
            and value(snapshot, points["fault_suppressed"]) == false
            and value(snapshot, points["fault_unack"]) == false,
        )
        if owner is None:
            owner = pair.tick(duty_url, standby_url, failures)[1]
            failures.append(
                "the return to service did not land — out_of_service "
                f"reads {value(owner, points['fault_oos'])}, "
                f"suppressed "
                f"{value(owner, points['fault_suppressed'])}, the "
                f"latch {value(owner, points['fault_unack'])}"
            )
            raise Abort
        unshelve = write_value(points["lal_shelve"], False)
        submit(standby_url, unshelve, failures)
        _tracked, owner = pair.tick(duty_url, standby_url, failures)
        if value(owner, points["lal_shelve"]) != false or value(
            owner, points["lal_shelved"]
        ) != false:
            failures.append(
                "the released shelve request left the surface "
                f"standing — the request point reads "
                f"{value(owner, points['lal_shelve'])}, shelved "
                f"{value(owner, points['lal_shelved'])}"
            )
            raise Abort
        receipts_duty = pair.get(f"{duty_url}/receipts", "GET /receipts", failures)
        receipts_standby = pair.get(
            f"{standby_url}/receipts", "GET /receipts", failures
        )
        if receipts_duty != receipts_standby:
            failures.append(
                "the peers' receipt logs diverged across the switch — "
                "the adopted audit is not one log"
            )
            raise Abort
        for command in (service_write, unshelve):
            if not settled(receipts_standby, command):
                failures.append(
                    f"the restore write {command} never settled "
                    "applied into the adopted receipt log"
                )
                raise Abort
        evidence["restored_at"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "restore",
                "restored_at": evidence["restored_at"],
            }
        )

        # Phase 8 — the launch roles back: demote the new owner,
        # promote the reconverged peer, and drive the pair to the
        # manifest's declared arrangement — the duty controller
        # `active`, its standby `tracking`.
        demote = rig.demote(standby_url, failures, "the new field owner")
        promote = rig.promote(duty_url, failures, "the reconverged peer")
        restore_ticks = []
        for _ in range(RESTORE_TICKS):
            _tracked, owner = pair.tick(standby_url, duty_url, failures)
            restore_ticks.append(owner["tick"])
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        standby_role = pair.get(f"{standby_url}/role", "GET /role", failures)
        if duty_role.get("role") != "active":
            failures.append(
                f"the restored field owner reports "
                f"{duty_role.get('role')!r}, expected active — the "
                "pair was not left in its declared roles"
            )
        sync = standby_role.get("sync")
        if standby_role.get("role") != "standby" or not (
            isinstance(sync, dict) and "tracking" in sync
        ):
            failures.append(
                "the restored standby never reconverged — GET /role "
                f"answers {standby_role}"
            )
        if failures:
            raise Abort
        evidence["roles_at"] = restore_ticks[-1]
        digest_entries.append(
            {
                "phase": "roles",
                "demote": demote,
                "promote": promote,
                "ticks": restore_ticks,
                "duty_role": duty_role,
                "standby_role": standby_role,
            }
        )

        # Phase 9 — the record: both durable journal files audited —
        # each carrying its single cold-start boundary with seq order
        # intact, the promoted peer's record ordering the adopted
        # settlements, the promotion's role entries inside the
        # countdown's span, the expiry at the asserted tick, and the
        # restores in run order; the served journal answering the same
        # record.
        files = {
            "duty": (rig.duty_files, duty_url),
            "standby": (rig.standby_files, standby_url),
        }
        audited = {}
        for name, (peer_files, url) in files.items():
            entries = journal_entries(peer_files, name, failures)
            served = pair.get(f"{url}/journal", "GET /journal", failures)
            events = journal_events(entries)
            if journal_events(served) != events:
                failures.append(
                    f"{name}'s served journal diverges from its "
                    "durable file — the monitor does not answer the "
                    "record it persists"
                )
                raise Abort
            audited[name] = {"entries": entries, "events": events}

        # The journaled edges measure the countdown twice. On the
        # demoted owner's record the assertion-to-expiry span is the
        # declared bound exactly — the applying scan counted as the
        # first shelved scan, its own and its tracking scans deriving
        # every edge on-tick. On the promoted peer's record the expiry
        # must land at the tick the continued countdown reached — the
        # receipt's applied tick plus `max_shelve_ticks` — never a
        # bound counted fresh from the promotion.
        edges = {
            name: [
                (entry["tick"], change.get("to"))
                for entry in audited[name]["entries"]
                for change in [entry.get("event", {}).get("point_changed", {})]
                if change.get("point") == points["lal_shelved"]
            ]
            for name in ("duty", "standby")
        }
        on = next((at for at, to in edges["duty"] if to == true), None)
        off = next(
            (
                at
                for at, to in edges["duty"]
                if to == false and at > (on or 0)
            ),
            None,
        )
        if on is None or off is None:
            failures.append(
                "the demoted owner's durable journal never recorded "
                "the shelve's assertion and expiry — the flag's edges "
                f"read {edges['duty']}"
            )
            raise Abort
        if on != shelved_at or off - on != bound:
            failures.append(
                f"the shelve spanned {off - on} scans between its "
                f"journaled assertion (tick {on}) and expiry (tick "
                f"{off}) — the receipt applied at tick {shelved_at} "
                f"and the declared bound is {bound}"
            )
            raise Abort
        adopted_on = next(
            (at for at, to in edges["standby"] if to == true), None
        )
        carried_off = next(
            (
                at
                for at, to in edges["standby"]
                if to == false and at > (adopted_on or 0)
            ),
            None,
        )
        if carried_off is None:
            failures.append(
                "the promoted peer's durable journal never recorded "
                "the shelve's expiry — the flag's edges read "
                f"{edges['standby']}"
            )
            raise Abort
        if carried_off != shelved_at + bound:
            failures.append(
                f"the promoted peer's journal expires the shelve at "
                f"tick {carried_off} — the continued countdown's "
                f"expiry is {shelved_at + bound} (the receipt applied "
                f"at tick {shelved_at}, the declared bound {bound}), "
                "a bound counted fresh from the promotion would "
                "expire later"
            )
            raise Abort

        promotion = {
            "duty": [
                ("role", "active", "demoting"),
                ("role", "demoting", "standby"),
            ],
            "standby": [
                ("role", "standby", "promoting"),
                ("role", "promoting", "active"),
            ],
        }
        restore = {
            "duty": [
                ("role", "standby", "promoting"),
                ("role", "promoting", "active"),
            ],
            "standby": [
                ("role", "active", "demoting"),
                ("role", "demoting", "standby"),
            ],
        }
        for name in ("duty", "standby"):
            events = audited[name]["events"]
            groups = [
                # The maintenance inhibit: the receipted oos write and
                # the declared wiring's managed states — the direct
                # `oos` landing ahead of the delivered `suppress`
                # copy.
                [
                    ("settled", points["pump_oos"], true, "applied", ACTOR),
                    ("changed", points["pump_oos"], true),
                    ("changed", points["fault_oos"], true),
                    ("changed", points["fault_suppressed"], true),
                ],
                # The held trip: the defeated feedback, the proven
                # fault, and `alarm` reporting process truth — no
                # `unacknowledged` transition beside it.
                [
                    ("quality", points["run"], BAD_QUALITY),
                    ("changed", points["fault"], true),
                    ("changed", points["fault_alarm"], true),
                ],
                # The mid-run shelve: the receipted request, the
                # journaled request point, the reporting flag.
                [
                    ("settled", points["lal_shelve"], true, "applied", ACTOR),
                    ("changed", points["lal_shelve"], true),
                    ("changed", points["lal_shelved"], true),
                ],
                # The switch inside the countdown's span.
                promotion[name],
                # The continued bound's own expiry while the request
                # still stands.
                [("changed", points["lal_shelved"], false)],
                # The cleared contact's return.
                [
                    ("quality", points["run"], "good"),
                    ("changed", points["fault"], false),
                    ("changed", points["fault_alarm"], false),
                ],
                # The return to service and the released request.
                [
                    ("settled", points["pump_oos"], false, "applied", ACTOR),
                    ("changed", points["pump_oos"], false),
                    ("changed", points["fault_oos"], false),
                    ("changed", points["fault_suppressed"], false),
                ],
                [
                    ("settled", points["lal_shelve"], false, "applied", ACTOR),
                    ("changed", points["lal_shelve"], false),
                ],
                # The launch roles back.
                restore[name],
            ]
            failures.extend(
                f"{name}'s journal: {miss}"
                for miss in managed_lifecycle.ordered_group_misses(
                    events, groups
                )
            )
            if ("changed", points["fault_unack"], true) in events:
                failures.append(
                    f"{name}'s journal latches unacknowledged through "
                    "the held trip — the carried suppression let the "
                    "standing fault annunciate"
                )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "audit",
                "duty_events": audited["duty"]["events"],
                "standby_events": audited["standby"]["events"],
                "expiry_tick": carried_off,
            }
        )
        evidence["entries"] = len(audited["standby"]["entries"])
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
        choices=["restarted-bound", "dropped-oos"],
        help="doctor the leg's own expectation — the pass must fail "
        "naming the evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = carryover_pass(args, args.tamper)
    except Abort as abort:
        for line in abort.args:
            eprint(f"carry: {line}")
        return 1
    for failure in failures:
        eprint(f"carry: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"carry: the {args.tamper} case passed silently — the "
                "leg never noticed the doctored expectation"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"carry-digest {digest} — tracking by tick "
        f"{evidence['converged']}, shelved at tick "
        f"{evidence['shelved_at']}, switched at tick "
        f"{evidence['switched_at']}, released at tick "
        f"{evidence['released_at']} on the declared bound, "
        "out-of-service held through the switch, roles restored at "
        f"tick {evidence['roles_at']}, {evidence['entries']} journal "
        "entries"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
