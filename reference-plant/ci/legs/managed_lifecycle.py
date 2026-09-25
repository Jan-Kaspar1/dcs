#!/usr/bin/env python3
"""The pair contract's managed-alarm lifecycle leg — the emitted
model's declared managed surface exercised end to end on the deployed
consumer pair.

ci/check.sh runs this script twice against the same declared
deployment as ci/legs/pair.py — `dcs-plant-server` serving the emitted
model and dynamics, plus the two manifest-declared `dcs-controller
--driven --remote` peers, the standby wired at the field owner's
monitor. With the pair converged and tracking, the leg drives the
managed-alarm lifecycle the emitted model declares (WW-ENG-003,
WW-ALM-001, WW-ALM-002):

- a field-held condition — the injected non-Good on `level-primary`
  the fault surface holds — fails the measurement over so the
  `backup-active` managed Bool alarm annunciates: `alarm` and
  `unacknowledged` asserting on the active's monitor with the
  journaled `point_changed` record;
- the managed `ack` through the receipted command path — the
  `accepted` submission settling `applied` under the leg's declared
  actor, the `unacknowledged` latch clearing;
- a bounded shelve on the shelvable low-level alarm through its
  writable `shelve` point — `shelved` reporting while the request
  stands, the auto-release landing at the emitted model's declared
  `max_shelve_ticks` with the request still held, the journaled
  assertion and expiry ticks measuring the bound exactly;
- a shelve write against the never-shelvable high-level alarm's
  bound-but-unwritable `shelve` point — the named `not_writable`
  refusal on the attributed receipt, journaled as a `command_settled`
  rejection, no state changing;
- the pump's `oos` maintenance inhibit through its writable point —
  the fault alarm's declared `out_of_service`/`suppress` wiring
  (decision 73) reporting `out_of_service`/`suppressed` while the
  alarms the model wires without those inputs report neither; then a
  driven run-contact fault proving `alarm` still reports the process
  truth while suppression withholds `unacknowledged`;
- the return to service — the standing condition arriving as a fresh
  `unacknowledged` transition when suppression releases, the
  receipted `ack` clearing it, the field fault cleared.

Every commanded transition settles through the receipted path with
the leg's actor attribution; the pair's roles never move; every
driven input is restored for later legs. The durable journal file
and the served journal must answer the same transition stream, the
ordered audit asserting each lifecycle entry lands in run order —
the receipts, the journaled point and quality transitions, and the
shelve's assertion-to-expiry span measuring `max_shelve_ticks`
exactly.

On success one `managed-lifecycle-digest <sha256>` line prints — the
check runs two passes and compares them. Every assertion failure
collects onto stderr prefixed `managed-lifecycle:` and exits 1 —
the check names it managed-lifecycle-failed; differing digests name
managed-lifecycle-nondeterministic.

`--tamper` doctors the leg's own expectation so a doctored
implementation — a shelve applying on the never-shelvable alarm, or
an auto-release missing the declared bound — would pass while the
honest run reports the named failure:

- `expect-applied` asserts the never-shelvable shelve write settles
  `applied` — the honest `not_writable` refusal fails it;
- `expect-standing` asserts the `shelved` flag still stands after
  the bound's own auto-release — the honest release fails it.

Both exit 1 like any failure: the check asserts each reports the
named diagnostic rather than passing silently.
"""

import argparse
import hashlib
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
# The doctored cases: a leg asserting the never-shelvable shelve
# write settled applied — shelving landing where the model
# declares none — and a leg asserting the shelved flag still
# stands after the declared bound's auto-release must each
# surface the named diagnostic rather than passing silently.
LEG = {
    "order": 210,
    "title": "the managed-alarm lifecycle leg",
    "passes": "managed-lifecycle",
    "tampers": [
        {
            "name": "expect-applied",
            "passed": "a expect-applied case passed the managed-lifecycle leg",
            "missed": "the expect-applied case did not report its named diagnostic",
            "evidence": ["expected an applied receipt"],
        },
        {
            "name": "expect-standing",
            "passed": "a expect-standing case passed the managed-lifecycle leg",
            "missed": "the expect-standing case did not report its named diagnostic",
            "evidence": ["still standing"],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The bound each driven phase gets to land its declared effect across
# the wiring's one-scan carrier crossings. The actor the leg's
# receipted submissions declare — the attribution every settled
# receipt, applied or refused, must carry.
SETTLE_BOUND = 16
ACTOR = "ci-managed-lifecycle"

# The injected non-Good the fault surface carries — the same quality
# the burst leg's cascade and the takeover leg's protection drive use.
BAD_QUALITY = {"bad": "device_fault"}

# The signal names resolving the leg's point ids out of the emitted
# model — the same names the signal index serves, so the leg exercises
# the declared seam, never a hard-coded id.
SIGNALS = {
    "level_primary": "level-primary",
    "backup_active": "backup-active",
    "backup_ack": "backup-active-ack",
    "backup_alarm": "backup-active-alarm",
    "backup_unack": "backup-active-unacknowledged",
    "lal_shelve": "lal-shelve",
    "lal_shelved": "lal-shelved",
    "lah_shelve": "lah-shelve",
    "lah_shelved": "lah-shelved",
    "pump_oos": "p101-oos",
    "run": "p101-run",
    "fault": "p101-fault",
    "fault_ack": "p101-fault-ack",
    "fault_alarm": "p101-fault-alarm",
    "fault_unack": "p101-fault-unacknowledged",
    "fault_shelved": "p101-fault-shelved",
    "fault_suppressed": "p101-fault-suppressed",
    "fault_oos": "p101-fault-out-of-service",
    "thermal_suppressed": "p101-thermal-suppressed",
    "thermal_oos": "p101-thermal-out-of-service",
    "moisture_suppressed": "p101-moisture-suppressed",
    "moisture_oos": "p101-moisture-out-of-service",
}

# The keys whose points must be model-declared writable — the
# receipted seams the leg commands.
WRITABLE = ("backup_ack", "lal_shelve", "pump_oos", "fault_ack")


def signal_points(model):
    """The `{key: point id}` map the emitted model's signal index
    declares for the leg's lifecycle — None when the model declares
    no such surface. A signal's `source` is the point it names; the
    lowest-signal-id-wins rule the served index applies. The receipted
    seams must be declared writable `In` points and the
    never-shelvable shelve point must be declared but unmarked — the
    `not_writable` surface the leg asserts."""
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
    if any(points[key] not in writable for key in WRITABLE):
        return None
    if points["lah_shelve"] in writable:
        return None
    return points


def shelve_bound(model, shelve_point):
    """The `max_shelve_ticks` the emitted model declares for the
    component whose `shelve` port binds `shelve_point` — the bound the
    auto-release must measure, resolved from the artifact rather than
    restated. None when no component binds the point's shelve port."""
    for connection in model.get("connections", []):
        port = connection.get("to", {}).get("port", {})
        if (
            connection.get("from", {}).get("point") == shelve_point
            and port.get("name") == "shelve"
        ):
            for component in model.get("components", []):
                if component.get("id") == port.get("component"):
                    parameter = component.get("parameters", {}).get(
                        "max_shelve_ticks", {}
                    )
                    return parameter.get("int")
    return None


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
    applied."""
    return any(
        entry.get("command") == command
        and simulate.receipt_outcome(entry) == "applied"
        for entry in receipts
    )


def tick(rig, failures):
    """One driven pair tick — the tracking peer scanned first so its
    pull applies the owner's latest checkpoint, then the field owner —
    asserting the peers' images stay identical. Returns the owner's
    served snapshot."""
    return rig.tick(rig.standby_url, rig.duty_url, failures)[1]


def drive_until(rig, failures, condition, bound=SETTLE_BOUND):
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
    record's `seq` order after the previous group's — the leg's
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
                    f"the journal carries no {want} at or after "
                    f"group {index}'s position — the lifecycle's "
                    "attributed transition is missing or out of order"
                )
            else:
                positions.append(position)
        if positions:
            cursor = max(positions) + 1
    return failures


def lifecycle_pass(args, tamper):
    """The managed-lifecycle run: converge, activate, ack, restore,
    shelve, refuse, inhibit, suppress, return, audit. Returns
    `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the lifecycle leg "
            "has nothing to exercise"
        )
    with open(args.model) as handle:
        model = json.load(handle)
    points = signal_points(model)
    if points is None:
        raise Abort(
            "the emitted model declares no managed-alarm lifecycle "
            "seam — the leg has nothing to exercise"
        )
    bound = shelve_bound(model, points["lal_shelve"])
    if bound is None:
        raise Abort(
            "no component's shelve port binds the declared lal shelve "
            "point — the model's max_shelve_ticks is unresolvable"
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
        quiet = (
            "backup_alarm",
            "backup_unack",
            "lal_shelved",
            "lah_shelved",
            "fault_oos",
            "fault_suppressed",
        )
        for key in quiet:
            if value(owner, points[key]) != false:
                failures.append(
                    f"the converged pair is not quiet — {key} reads "
                    f"{value(owner, points[key])} before the leg drives"
                )
        if failures:
            raise Abort

        # Phase 2 — the field-driven activation: the injected non-Good
        # on `level-primary` fails the measurement over, the
        # backup-serving flag asserts the declared managed Bool alarm's
        # `in`, and `alarm`/`unacknowledged` annunciate on the active's
        # monitor — the journaled `point_changed` record the audit
        # asserts.
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
            lambda snapshot: value(snapshot, points["backup_active"]) == true
            and value(snapshot, points["backup_alarm"]) == true
            and value(snapshot, points["backup_unack"]) == true,
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                "the field-held fault never annunciated — "
                f"backup-active reads "
                f"{value(owner, points['backup_active'])}, alarm "
                f"{value(owner, points['backup_alarm'])}, "
                f"unacknowledged "
                f"{value(owner, points['backup_unack'])}"
            )
            raise Abort
        evidence["annunciated_at"] = owner["tick"]

        # Phase 3 — the receipted ack: the managed `ack` write submits
        # `accepted`, settles `applied` under the leg's actor, and the
        # latch clears — then the released request restores the input.
        ack_write = write_value(points["backup_ack"], True)
        submit(duty_url, ack_write, failures)
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: value(snapshot, points["backup_unack"]) == false,
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                "the receipted ack never cleared the latch — "
                f"unacknowledged reads "
                f"{value(owner, points['backup_unack'])}"
            )
            raise Abort
        receipts = pair.get(f"{duty_url}/receipts", "GET /receipts", failures)
        if not settled(receipts, ack_write):
            failures.append(
                "the ack write never settled applied into the adopted "
                "receipt log"
            )
            raise Abort
        evidence["acknowledged_at"] = owner["tick"]
        release = write_value(points["backup_ack"], False)
        submit(duty_url, release, failures)
        owner = tick(rig, failures)

        # Phase 4 — the field restore: the cleared instrument returns
        # the failover's selection, the standing alarm reports clear,
        # and the acknowledged latch stays down.
        verdict = plant_io.request(
            {"op": "clear_fault", "point": points["level_primary"]}
        )
        if verdict.get("result") != "done":
            failures.append(f"clear_fault on level-primary answered {verdict}")
            raise Abort
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: value(snapshot, points["backup_active"]) == false
            and value(snapshot, points["backup_alarm"]) == false
            and value(snapshot, points["backup_unack"]) == false,
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                "the cleared instrument left the alarm standing — "
                f"backup-active reads "
                f"{value(owner, points['backup_active'])}, alarm "
                f"{value(owner, points['backup_alarm'])}, "
                f"unacknowledged "
                f"{value(owner, points['backup_unack'])}"
            )
            raise Abort
        evidence["returned_at"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "activation",
                "annunciated_at": evidence["annunciated_at"],
                "acknowledged_at": evidence["acknowledged_at"],
                "returned_at": evidence["returned_at"],
            }
        )

        # Phase 5 — the bounded shelve: the shelvable alarm's writable
        # `shelve` point takes the receipted request, `shelved` reports
        # while it stands, and the declared `max_shelve_ticks` bound
        # releases it with the request still held — the flag's
        # journaled assertion and expiry measuring the bound exactly.
        shelve_write = write_value(points["lal_shelve"], True)
        submit(duty_url, shelve_write, failures)
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: value(snapshot, points["lal_shelved"]) == true,
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                "the receipted shelve never reported shelved — "
                f"shelved reads "
                f"{value(owner, points['lal_shelved'])}, the request "
                f"point {value(owner, points['lal_shelve'])}"
            )
            raise Abort
        evidence["shelved_at"] = owner["tick"]
        released = None
        for _ in range(bound + SETTLE_BOUND):
            owner = tick(rig, failures)
            if value(owner, points["lal_shelved"]) == false:
                released = owner
                break
        if released is None:
            failures.append(
                f"the bounded shelve never released — shelved still "
                f"reads true {bound + SETTLE_BOUND} scans after the "
                f"request landed, the declared bound {bound}"
            )
            raise Abort
        if value(released, points["lal_shelve"]) != true:
            failures.append(
                "the shelve released with its request withdrawn — "
                f"the request point reads "
                f"{value(released, points['lal_shelve'])}, not the "
                "bound's own auto-release"
            )
            raise Abort
        if tamper == "expect-standing":
            failures.append(
                "the bounded shelve released at the declared bound — "
                "expected the flag still standing past "
                "max_shelve_ticks"
            )
            raise Abort
        evidence["released_at"] = released["tick"]
        unshelve = write_value(points["lal_shelve"], False)
        submit(duty_url, unshelve, failures)
        owner = tick(rig, failures)
        if value(owner, points["lal_shelve"]) != false or value(
            owner, points["lal_shelved"]
        ) != false:
            failures.append(
                "the released shelve request left the surface standing "
                f"— the request point reads "
                f"{value(owner, points['lal_shelve'])}, shelved "
                f"{value(owner, points['lal_shelved'])}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "shelve",
                "bound": bound,
                "shelved_at": evidence["shelved_at"],
                "released_at": evidence["released_at"],
            }
        )

        # Phase 6 — the never-shelvable refusal: the high-level alarm's
        # shelve point is bound but declared unwritable, so the write
        # answers the named `not_writable` on the attributed receipt —
        # the journaled `command_settled` rejection — and no state
        # changes.
        status, receipt = pair.request(
            f"{duty_url}/command",
            {
                "command": write_value(points["lah_shelve"], True),
                "actor": ACTOR,
            },
        )
        if tamper == "expect-applied":
            if status != 200 or simulate.receipt_outcome(receipt) != "applied":
                failures.append(
                    f"the never-shelvable shelve write answered "
                    f"{status} {receipt} — expected an applied receipt"
                )
                raise Abort
        else:
            if (
                status != 200
                or simulate.receipt_outcome(receipt) != "not_writable"
            ):
                failures.append(
                    f"the never-shelvable shelve write answered "
                    f"{status} {receipt}, expected the named "
                    "not_writable refusal"
                )
                raise Abort
            if receipt.get("actor") != ACTOR:
                failures.append(
                    f"the refusal carried actor "
                    f"{receipt.get('actor')!r}, not the leg's declared "
                    f"{ACTOR}"
                )
                raise Abort
        owner = tick(rig, failures)
        if value(owner, points["lah_shelve"]) != false or value(
            owner, points["lah_shelved"]
        ) != false:
            failures.append(
                "the refused shelve write changed state — the request "
                f"point reads {value(owner, points['lah_shelve'])}, "
                f"shelved {value(owner, points['lah_shelved'])}"
            )
            raise Abort
        evidence["refused_at"] = owner["tick"]

        # Phase 7 — the designed suppression: the pump's `oos` point
        # takes the receipted inhibit; the fault alarm's declared
        # `oos`/`suppress` wiring reports `out_of_service` and
        # `suppressed` while the alarms the model wires without those
        # inputs report neither.
        oos_write = write_value(points["pump_oos"], True)
        submit(duty_url, oos_write, failures)
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: value(snapshot, points["fault_oos"]) == true
            and value(snapshot, points["fault_suppressed"]) == true,
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                "the maintenance inhibit never reported — "
                f"out_of_service reads "
                f"{value(owner, points['fault_oos'])}, suppressed "
                f"{value(owner, points['fault_suppressed'])}"
            )
            raise Abort
        for key in (
            "thermal_suppressed",
            "thermal_oos",
            "moisture_suppressed",
            "moisture_oos",
        ):
            if value(owner, points[key]) != false:
                failures.append(
                    "the maintenance inhibit leaked onto an alarm the "
                    f"model wires without it — {key} reads "
                    f"{value(owner, points[key])}"
                )
        if failures:
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

        # Phase 8 — the process truth under suppression: the injected
        # non-Good on the run contact defeats the feedback's proof, the
        # motor's `fault` asserts the alarm's `in`, and `alarm` reports
        # the standing truth while suppression withholds the latch.
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
            failures,
            lambda snapshot: value(snapshot, points["fault"]) == true
            and value(snapshot, points["fault_alarm"]) == true
            and value(snapshot, points["fault_unack"]) == false
            and value(snapshot, points["fault_suppressed"]) == true,
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                "the suppressed trip never reported — fault reads "
                f"{value(owner, points['fault'])}, alarm "
                f"{value(owner, points['fault_alarm'])}, "
                f"unacknowledged "
                f"{value(owner, points['fault_unack'])}, suppressed "
                f"{value(owner, points['fault_suppressed'])}"
            )
            raise Abort
        evidence["suppressed_trip_at"] = owner["tick"]

        # Phase 9 — the return to service: the receipted restore drops
        # the inhibit, the delivered suppress copy follows a carrier
        # crossing later, and the condition that outlasted its
        # suppression arrives as a fresh `unacknowledged` transition —
        # cleared by the receipted `ack` before the field fault clears.
        service_write = write_value(points["pump_oos"], False)
        submit(duty_url, service_write, failures)
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: value(snapshot, points["fault_oos"]) == false
            and value(snapshot, points["fault_suppressed"]) == false
            and value(snapshot, points["fault_unack"]) == true,
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                "the return to service did not land — out_of_service "
                f"reads {value(owner, points['fault_oos'])}, suppressed "
                f"{value(owner, points['fault_suppressed'])}, the "
                f"fresh trip's latch "
                f"{value(owner, points['fault_unack'])}"
            )
            raise Abort
        evidence["service_at"] = owner["tick"]
        fault_ack = write_value(points["fault_ack"], True)
        submit(duty_url, fault_ack, failures)
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: value(snapshot, points["fault_unack"]) == false,
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                "the receipted fault ack never cleared the latch — "
                f"unacknowledged reads "
                f"{value(owner, points['fault_unack'])}"
            )
            raise Abort
        submit(duty_url, write_value(points["fault_ack"], False), failures)
        owner = tick(rig, failures)
        verdict = plant_io.request(
            {"op": "clear_fault", "point": points["run"]}
        )
        if verdict.get("result") != "done":
            failures.append(f"clear_fault on the run contact answered {verdict}")
            raise Abort
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: value(snapshot, points["fault"]) == false
            and value(snapshot, points["fault_alarm"]) == false
            and value(snapshot, points["fault_unack"]) == false,
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                "the cleared contact left the fault standing — fault "
                f"reads {value(owner, points['fault'])}, alarm "
                f"{value(owner, points['fault_alarm'])}, "
                f"unacknowledged "
                f"{value(owner, points['fault_unack'])}"
            )
            raise Abort
        evidence["restored_at"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "suppression",
                "oos_at": evidence["oos_at"],
                "suppressed_trip_at": evidence["suppressed_trip_at"],
                "service_at": evidence["service_at"],
                "restored_at": evidence["restored_at"],
            }
        )

        # Phase 10 — the restoration and the record: the pair's roles
        # never moved, every driven input reads restored, the peers'
        # adopted receipt logs are identical, and the field owner's
        # durable journal file carries the lifecycle in run order —
        # the served journal answering the same record.
        standby_role = pair.get(f"{standby_url}/role", "GET /role", failures)
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        sync = standby_role.get("sync")
        if standby_role.get("role") != "standby" or not (
            isinstance(sync, dict) and "tracking" in sync
        ):
            failures.append(
                "the standby's role moved through the lifecycle — GET "
                f"/role answers {standby_role}"
            )
        if duty_role.get("role") != "active":
            failures.append(
                "the field owner's role moved through the lifecycle — "
                f"GET /role answers {duty_role}"
            )
        for key in ("backup_ack", "lal_shelve", "pump_oos", "fault_ack"):
            if value(owner, points[key]) != false:
                failures.append(
                    f"the leg left {key} standing — the point reads "
                    f"{value(owner, points[key])} at restore"
                )
        if failures:
            raise Abort
        receipts_duty = pair.get(f"{duty_url}/receipts", "GET /receipts", failures)
        receipts_standby = pair.get(
            f"{standby_url}/receipts", "GET /receipts", failures
        )
        if receipts_duty != receipts_standby:
            failures.append(
                "the peers' receipt logs diverged — the adopted audit "
                "trail is not identical on both peers"
            )
            raise Abort

        journal_path = rig.duty_files.get("journal_file")
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

        # The shelve's journaled edges measure the declared bound: the
        # request's asserting scan counts as the first shelved scan, so
        # the flag's assertion-to-expiry span is `max_shelve_ticks`
        # exactly.
        edges = [
            (entry["tick"], change.get("to"))
            for entry in entries
            for change in [entry.get("event", {}).get("point_changed", {})]
            if change.get("point") == points["lal_shelved"]
        ]
        on = next((at for at, to in edges if to == true), None)
        off = next(
            (at for at, to in edges if to == false and at > (on or 0)),
            None,
        )
        if on is None or off is None:
            failures.append(
                f"the durable journal never recorded the shelve's "
                f"assertion and expiry — the flag's edges read {edges}"
            )
            raise Abort
        if off - on != bound:
            failures.append(
                f"the shelve spanned {off - on} scans between its "
                f"journaled assertion (tick {on}) and expiry (tick "
                f"{off}) — the declared bound is {bound}"
            )
            raise Abort

        groups = [
            # The field-driven activation: the failed quality, the
            # backup-serving flag, and the managed alarm's
            # annunciation.
            [
                ("quality", points["level_primary"], BAD_QUALITY),
                ("changed", points["backup_active"], true),
                ("changed", points["backup_alarm"], true),
                ("changed", points["backup_unack"], true),
            ],
            # The receipted ack: the applied settlement under the
            # leg's actor and the cleared latch.
            [
                ("settled", points["backup_ack"], true, "applied", ACTOR),
                ("changed", points["backup_unack"], false),
            ],
            # The released request and the cleared instrument's
            # return.
            [
                ("settled", points["backup_ack"], false, "applied", ACTOR),
                ("quality", points["level_primary"], "good"),
                ("changed", points["backup_active"], false),
                ("changed", points["backup_alarm"], false),
            ],
            # The bounded shelve: the receipted request, the journaled
            # request point, the reporting flag — then the bound's own
            # expiry while the request still stands.
            [
                ("settled", points["lal_shelve"], true, "applied", ACTOR),
                ("changed", points["lal_shelve"], true),
                ("changed", points["lal_shelved"], true),
            ],
            [("changed", points["lal_shelved"], false)],
            [
                ("settled", points["lal_shelve"], false, "applied", ACTOR),
                ("changed", points["lal_shelve"], false),
            ],
            # The never-shelvable refusal: the named rejection
            # journaled as a settled command under the leg's actor —
            # no state transition beside it.
            [
                (
                    "settled",
                    points["lah_shelve"],
                    true,
                    "not_writable",
                    ACTOR,
                ),
            ],
            # The maintenance inhibit: the receipted oos write and the
            # declared wiring's managed states — the direct `oos`
            # landing ahead of the delivered `suppress` copy.
            [
                ("settled", points["pump_oos"], true, "applied", ACTOR),
                ("changed", points["pump_oos"], true),
                ("changed", points["fault_oos"], true),
                ("changed", points["fault_suppressed"], true),
            ],
            # The suppressed trip: the defeated feedback, the proven
            # fault, and the alarm reporting process truth — no
            # `unacknowledged` transition beside it.
            [
                ("quality", points["run"], BAD_QUALITY),
                ("changed", points["fault"], true),
                ("changed", points["fault_alarm"], true),
            ],
            # The return to service: the restore's settlement, the
            # direct `oos` drop, then the delivered `suppress` copy's
            # release evaluating the standing condition as a fresh
            # trip — the latch's transition written after the managed
            # outputs' in the same scan.
            [
                ("settled", points["pump_oos"], false, "applied", ACTOR),
                ("changed", points["pump_oos"], false),
                ("changed", points["fault_oos"], false),
                ("changed", points["fault_suppressed"], false),
                ("changed", points["fault_unack"], true),
            ],
            # The receipted ack clearing the fresh trip, then the
            # cleared contact's return.
            [
                ("settled", points["fault_ack"], true, "applied", ACTOR),
                ("changed", points["fault_unack"], false),
                ("settled", points["fault_ack"], false, "applied", ACTOR),
            ],
            [
                ("quality", points["run"], "good"),
                ("changed", points["fault"], false),
                ("changed", points["fault_alarm"], false),
            ],
        ]
        failures.extend(ordered_group_misses(events, groups))
        if failures:
            raise Abort
        digest_entries.append(
            {"phase": "audit", "events": events, "shelve_span": off - on}
        )
        evidence["entries"] = len(entries)
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
        choices=["expect-applied", "expect-standing"],
        help="doctor the leg's own expectation — the pass must fail "
        "naming the evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = lifecycle_pass(args, args.tamper)
    except Abort as abort:
        for line in abort.args:
            eprint(f"managed-lifecycle: {line}")
        return 1
    for failure in failures:
        eprint(f"managed-lifecycle: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"managed-lifecycle: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored "
                "expectation"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"managed-lifecycle-digest {digest} — tracking by tick "
        f"{evidence['converged']}, annunciated at tick "
        f"{evidence['annunciated_at']}, acknowledged at tick "
        f"{evidence['acknowledged_at']}, shelved at tick "
        f"{evidence['shelved_at']}, released at tick "
        f"{evidence['released_at']}, restored at tick "
        f"{evidence['restored_at']}, {evidence['entries']} journal "
        "entries"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
