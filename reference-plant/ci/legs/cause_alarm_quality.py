#!/usr/bin/env python3
"""The pair contract's quality-aware cause-alarm leg — the consumer
boundary's mirror of the qa-rig leg at
qa_lane/scenarios/3960_cause_alarm_quality.py, proving on the released
images that a protection contact degraded enough to trip the pump's
fail-safe protection annunciates its declared cause alarm rather than
stopping the pump silently (WW-ENG-003, WW-ALM-001, WW-OPS-003).

ci/check.sh runs this script twice against the same declared
deployment as ci/legs/pair.py — `dcs-plant-server` serving the emitted
model and dynamics, plus the two manifest-declared `dcs-controller
--driven --remote` peers, the standby wired at the field owner's
monitor. The consumer model wires each pump's thermal and moisture
contacts two ways: straight into the protection aggregator's trip
ports, and into a single-trip `interlock` cause guard whose `tripped`
drives that contact's managed cause alarm. The leg resolves that seam
out of the artifact — never a hard-coded point id — and runs:

- the declared pump held in manual under a receipted hand request,
  until the field's own stored motor command reads energized;
- the degraded contact: an `inject_fault` of
  `{quality: {bad: device_fault}}` through the dedicated plant
  connection presents the substituted quality over the standing stored
  field value, trips the protection fail-safe — `protect-tripped`
  standing, `protections-ok` dropped, the aggregated availability no
  longer proven at Good — and annunciates the matching cause alarm's
  `alarm` and `unacknowledged` with its managed outputs down, while the
  sibling contact's alarm and the pump's motor-fault alarm stay clean
  and the field command de-energizes: never a silent protective stop,
  never a duplicate trip;
- the declared recovery: `clear_fault` returns the contact to Good, the
  trip clears, the condition reports clear while the latch stands, and
  the held hand request re-energizes the field command; a receipted
  `ack` press clears the latch through the consumed edge and the
  released request re-arms the input;
- the honest-absence halves: a Good-quality value trip annunciates
  identically with no quality transition beside it in the record, and a
  degraded contact on the stopped pump annunciates its own cause alarm
  while the pump neither starts nor plants any alarm beyond the
  declared contract;
- the restore: every driven input and every latch returns, both peers'
  receipt logs stay identical, the pair's roles never move, and the
  field owner's durable journal carries each drive's injected quality,
  its trip, and its annunciation in `seq` order beside the attributed
  settlements.

Where the consumer model declares no quality-gated protection contact —
no guard between a contact and its cause alarm — the leg reports
inconclusive naming the absent declared surface rather than passing
vacuously. Where the pinned release predates the contract — its served
registry reports no such cause guard — the leg reports inconclusive,
never a product failure.

On success one `cause-alarm-quality-digest <sha256>` line prints —
the check runs two passes and compares them. Every assertion failure
collects onto stderr prefixed `cause-alarm-quality:` and exits 1 —
the check names it cause-alarm-quality-failed; differing digests name
cause-alarm-quality-nondeterministic.

`--tamper` doctors the leg's own expectations so a doctored
implementation — a cause alarm blind to the degraded contact, or a
protection that fails to cut the field — would pass while the honest
run reports the named failure:

- `expect-silent` asserts the cause alarm stays clean-false after the
  degraded contact — the pre-#827 defect's exact shape, which the
  honest annunciation fails;
- `expect-standing` asserts the field command still reads energized
  after the degraded contact, which the honest protective stop fails.

Both exit 1 like any failure: the check asserts each reports the
named diagnostic rather than passing silently.
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


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its two
# digest-identical passes, and exercise its doctored cases. The
# doctored cases: a leg asserting the cause alarm stayed silent over
# the degraded contact, and a leg asserting the field command held
# standing through the trip — each must surface the named diagnostic
# rather than passing silently.
LEG = {
    "order": 860,
    "title": "the quality-aware cause-alarm leg",
    "passes": "cause-alarm-quality",
    "tampers": [
        {
            "name": "expect-silent",
            "passed": "a expect-silent case passed the cause-alarm-quality leg",
            "missed": "the expect-silent case did not report its named diagnostic",
            "evidence": ["expected the cause alarm to stay silent"],
        },
        {
            "name": "expect-standing",
            "passed": "a expect-standing case passed the cause-alarm-quality leg",
            "missed": "the expect-standing case did not report its named diagnostic",
            "evidence": ["expected the field command to stand"],
        },
    ],
}


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The declared contract's surface is absent — the consumer model
    declares no quality-gated protection contact, or the pinned
    release predates the contract. The run classifies inconclusive,
    never a product failure."""


# The bound the hand-held pump gets to energize, the bound each driven
# phase gets to land its declared effect across the wiring's carrier
# crossings, and the actor the leg's receipted submissions declare.
START_BOUND = 32
SETTLE_BOUND = 16
ACTOR = "ci-cause-alarm"

# The substituted quality the leg's contact faults carry — the same
# one the managed-lifecycle and burst legs inject, in the durable
# record's own stamp and in the fault surface's `Fault` wrapper.
BAD_QUALITY = {"bad": "device_fault"}
BAD_FAULT = {"quality": BAD_QUALITY}

# The durable record's own wire shape for a Bool sample.
TRUE = {"bool": True}
FALSE = {"bool": False}

# The contact kinds the consumer model is expected to declare as
# quality-gated protection contacts.
CONTACTS = ("thermal", "moisture")

# The managed flag outputs a cause alarm stands on — declared quiet by
# the contract, so a record on any of them is a violation — as
# (leg key, signal-name suffix) pairs.
MANAGED = (("shelved", "shelved"), ("suppressed", "suppressed"),
           ("out_of_service", "out-of-service"))

MANAGED_KINDS = ("managed-bool-latching-alarm", "managed-latching-alarm")

# The signal names resolving the leg's point ids out of the emitted
# model — the same names the signal index serves, so the leg exercises
# the declared seam, never a hard-coded id.
SEAMS = {
    "mode": "p101-mode",
    "hand": "p101-hand",
    "cmd": "p101-cmd",
    "avail": "p101-avail",
    "protect": "p101-protect-tripped",
    "protect_ok": "p101-protections-ok",
    "fault": "p101-fault",
    "fault_alarm": "p101-fault-alarm",
    "fault_unack": "p101-fault-unacknowledged",
}
for _kind in CONTACTS:
    SEAMS[_kind] = f"p101-{_kind}"
    SEAMS[f"{_kind}_ack"] = f"p101-{_kind}-ack"
    SEAMS[f"{_kind}_alarm"] = f"p101-{_kind}-alarm"
    SEAMS[f"{_kind}_unack"] = f"p101-{_kind}-unacknowledged"
    for _key, _suffix in MANAGED:
        SEAMS[f"{_kind}_{_key}"] = f"p101-{_kind}-{_suffix}"
for _key, _suffix in MANAGED:
    SEAMS[f"fault_{_key}"] = f"p101-fault-{_suffix}"

# The keys whose points must be model-declared writable — the
# receipted seams the leg commands.
WRITABLE = ("mode", "hand") + tuple(
    f"{kind}_ack" for kind in CONTACTS
)


def signal_points(model):
    """The `{key: point id}` map the emitted model's signal index
    declares for the leg's seam — the lowest-signal-id-wins rule the
    served index applies. None when the model declares no such
    signal."""
    by_name = {}
    for signal in model.get("signals", []):
        current = by_name.get(signal["name"])
        if current is None or signal["id"] < current[0]:
            by_name[signal["name"]] = (signal["id"], signal["source"])
    points = {}
    for key, signal_name in SEAMS.items():
        entry = by_name.get(signal_name)
        points[key] = entry[1] if entry else None
    if any(point is None for point in points.values()):
        return None
    return points


def declared_points(model):
    """`{point id: io_point}` — the model's declared points."""
    return {point["id"]: point for point in model.get("io_points", [])}


def quality_gated_seam(model, points):
    """`{contact kind: {"alarm", "guard", "in"}}` — the declared cause
    guards: the managed alarm whose `alarm` output binds the contact's
    alarm point, whose `in` binds a port-to-port wire out of a
    single-trip `interlock` guard's `tripped`, and whose own `trip_1`
    binds the raw contact. Raises Inconclusive naming the absent
    declared surface when the model wires no such guard — the shape
    where a degraded reading cannot annunciate."""
    bound = simulate.bound_points(model)
    kinds = {
        component["id"]: component["kind"]
        for component in model.get("components", [])
    }
    wired = {}
    owners = {}
    for connection in model.get("connections", []):
        source, target = connection.get("from", {}), connection.get("to", {})
        port = target.get("port")
        if port is not None:
            key = (port.get("component"), port.get("name"))
            if "point" in source:
                wired[key] = source["point"]
            elif "port" in source:
                wired[key] = (source["port"].get("component"),
                              source["port"].get("name"))
        served = source.get("port")
        if served is not None and served.get("name") == "alarm" \
                and "point" in target:
            owners.setdefault(target["point"], set()).add(
                served.get("component")
            )
    declared = declared_points(model)
    seam = {}
    for kind in CONTACTS:
        alarm_point = points[f"{kind}_alarm"]
        candidates = [
            ident
            for ident in owners.get(alarm_point, ())
            if kinds.get(ident) in MANAGED_KINDS
        ]
        if not candidates:
            raise Inconclusive(
                f"the emitted model binds no managed cause alarm's "
                f"`alarm` output to the {kind}-alarm point "
                f"{alarm_point}"
            )
        guard_of = None
        for owner in candidates:
            entry = wired.get((owner, "in"))
            if isinstance(entry, tuple) \
                    and kinds.get(entry[0]) == "interlock" \
                    and entry[1] == "tripped":
                guard_of = entry[0]
                break
        if guard_of is None:
            raise Inconclusive(
                f"the emitted model wires the {kind} cause alarm's "
                "condition to no single-trip interlock guard's "
                f"`tripped` — it declares no quality-gated protection "
                f"contact for {kind}"
            )
        if wired.get((guard_of, "trip_1")) != points[kind] \
                or bound.get((guard_of, "trip_1")) != points[kind]:
            raise Inconclusive(
                f"the emitted model's {kind} cause guard binds no raw "
                "contact on its trip input — the guard is not the "
                f"declared quality gate for {kind}"
            )
        contact = declared.get(points[kind], {})
        if contact.get("direction") != "in" \
                or contact.get("value_type") != "bool" \
                or contact.get("channel") is None \
                or not contact.get("journaled"):
            raise Inconclusive(
                f"the emitted model's {kind} contact point "
                f"{points[kind]} is not a journaled field Bool input"
            )
        seam[kind] = {
            "alarm": f"{kinds[candidates[0]]}:{candidates[0]}",
            "guard": f"interlock:{guard_of}",
            "in": bound.get((candidates[0], "in")),
        }
    return seam


def contract_probe(schema, seam, points):
    """The quality-aware cause-alarm contract the pinned release must
    carry: its served block-interface registry reports each cause guard
    and binds the cause alarm's `in` to the guard's carrier — never to
    the raw contact. A release predating the contract serves no such
    registry, and the leg classifies inconclusive rather than failing a
    release that never claimed the wiring."""
    served = {}
    for entry in schema.get("interfaces", []):
        served[entry.get("name")] = entry.get("interface", {})
    for kind in CONTACTS:
        guard = served.get(seam[kind]["guard"])
        alarm = served.get(seam[kind]["alarm"])
        if guard is None or guard.get("kind") != "interlock":
            return False
        if alarm is None or alarm.get("kind") not in MANAGED_KINDS:
            return False
        resources = {}
        for collection in ("measurements", "state"):
            for entry in alarm.get(collection, []):
                resources[entry.get("name")] = entry
        port = resources.get("in")
        if port is None or port.get("point") != seam[kind]["in"]:
            return False
        if seam[kind]["in"] == points[kind]:
            return False
    return True


def receipted_seam(model, points):
    """The leg's receipted drive points — every writable key the run
    commands must be declared writable in the emitted model. Raises
    Inconclusive naming the absent admission otherwise: the receipted
    path owes a writable point its submission, and a model that
    declares none cannot exercise it."""
    writable = {
        point["id"]
        for point in declared_points(model).values()
        if point.get("writable")
    }
    for key in WRITABLE:
        if points[key] not in writable:
            raise Inconclusive(
                f"the emitted model's {key.replace('_', ' ')} point "
                f"{points[key]} is not declared writable — the leg's "
                "receipted drive has no admission"
            )
    return points


def value(snapshot, point):
    """The point's latest sample value — `simulate.snapshot_point`."""
    return simulate.snapshot_point(snapshot, point)


def flag(snapshot, point):
    """The point's latest Bool sample as a truth value."""
    reading = value(snapshot, point) or {}
    return reading.get("bool") is True


def quality_of(snapshot, point):
    """The point's latest sample quality, in the comparable form the
    durable record uses — 'good' or the substituted `{bad: reason}`
    stamp."""
    for entry in snapshot.get("points", []):
        if entry.get("point") == point:
            quality = (entry.get("sample") or {}).get("quality")
            break
    else:
        return None
    if quality == "good" or quality is None:
        return quality
    if isinstance(quality, dict) and quality:
        kind, reason = next(iter(quality.items()))
        return {kind: reason}
    return quality


def proven(snapshot, point):
    """Whether one point reads `true` at Good — the fail-safe reading:
    a degraded contact's inverted serving cannot prove its condition."""
    return flag(snapshot, point) and quality_of(snapshot, point) == "good"


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
    """The adopted receipt log's applied entries for `command`, in
    submission order — identical submissions match positionally."""
    return [
        entry
        for entry in receipts
        if entry.get("command") == command
        and simulate.receipt_outcome(entry) == "applied"
    ]


def field_read(plant_io, point):
    """The field's stored value for one point read through the plant
    protocol — the scan-sequence probe for the driven output and the
    field's own account of the contact. None when the field answers no
    sample, so each caller's own comparison names the miss."""
    response = plant_io.request({"op": "read", "point": point})
    if response.get("result") != "sample":
        return None
    return (response.get("sample") or {}).get("value")


def owner_token(preamble):
    """The field owner's writer-claim owner token, as its startup
    preamble reports it — the token this leg's field attachment joins
    so its driven writes land inside the standing claim. None while
    the release records no claim, in which case the writes land
    unfenced."""
    for line in preamble:
        claimed = re.search(r"owner token (\d+)", line)
        if claimed:
            return int(claimed.group(1))
    return None


def join_claim(plant_io, token, failures):
    """Join the field owner's standing writer claim under its own
    recorded owner token — the shared-claim path the released server
    fences a third attachment's writes without."""
    if token is None:
        return None
    verdict = plant_io.request({"op": "ensure_writer", "owner": token})
    if verdict.get("result") not in ("done", "claimed_shared"):
        failures.append(
            f"the writer-claim join under the recorded owner token "
            f"{token} answered {verdict}"
        )
        raise Abort
    return verdict


def field_write(plant_io, point, boolean, failures, what):
    """One field-side `write` through the plant protocol — the
    unfenced diagnostic surface the pair-stage legs drive."""
    response = plant_io.request(
        {"op": "write", "point": point, "value": {"bool": boolean}}
    )
    if response.get("result") != "done":
        failures.append(f"{what} on point {point} answered {response}")
        raise Abort
    return response


def field_fault(plant_io, point, fault, failures, what):
    """One `inject_fault` or `clear_fault` through the dedicated plant
    connection, asserting the field's own verdict."""
    request = {"op": "inject_fault" if fault else "clear_fault",
               "point": point}
    if fault:
        request["fault"] = fault
    response = plant_io.request(request)
    if response.get("result") != "done":
        failures.append(f"{what} on point {point} answered {response}")
        raise Abort
    return response


def tick(rig, failures):
    """One driven pair tick — the tracking peer scanned first so its
    pull applies the owner's latest checkpoint, then the field owner —
    asserting the peers' images stay identical. Returns the owner's
    served snapshot."""
    return rig.tick(rig.standby_url, rig.duty_url, failures)[1]


def field_until(rig, failures, plant_io, point, want, bound=SETTLE_BOUND):
    """Drive the pair until the field's own stored value for `point`
    reads `want` — a field output moves only on a driven scan, so each
    probe follows one. True when the value lands, False when `bound`
    scans pass without it."""
    for _ in range(bound + 1):
        if field_read(plant_io, point) == want:
            return True
        tick(rig, failures)
    return field_read(plant_io, point) == want


def drive_until(rig, failures, condition, bound=SETTLE_BOUND):
    """Driven pair ticks until `condition(owner_snapshot)` holds —
    returns the satisfying snapshot, or None when `bound` scans pass
    without it landing. The wiring's carrier crossings take a few scans
    to arrive, so the bound names a landing that never did."""
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
    record's `seq` order after the previous group's — the leg's driven
    transitions and attributed settlements landing in run order.
    Returns the named misses."""
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
                    f"the durable journal carries no {want} at or after "
                    f"group {index}'s position — the driven transition "
                    "is missing or out of order"
                )
            else:
                positions.append(position)
        if positions:
            cursor = max(positions) + 1
    return failures


def alarm_standing(snapshot, points, kind, latched):
    """Whether the contact's cause alarm carries the declared
    annunciation: condition and `alarm` standing, the latch as asked,
    and every managed output down."""
    if not flag(snapshot, points[f"{kind}_alarm_in"]):
        return False
    if not flag(snapshot, points[f"{kind}_alarm"]):
        return False
    if flag(snapshot, points[f"{kind}_unack"]) is not latched:
        return False
    return not any(
        flag(snapshot, points[f"{kind}_{key}"]) for key, _ in MANAGED
    )


def alarm_clear(snapshot, points, kind, latched):
    """Whether the contact's cause alarm stands clear — the condition
    and `alarm` reporting clear, the latch as asked, and every managed
    output down."""
    if flag(snapshot, points[f"{kind}_alarm_in"]):
        return False
    if flag(snapshot, points[f"{kind}_alarm"]):
        return False
    if flag(snapshot, points[f"{kind}_unack"]) is not latched:
        return False
    return not any(
        flag(snapshot, points[f"{kind}_{key}"]) for key, _ in MANAGED
    )


def nothing_else(snapshot, points, kind):
    """The honest-absence half: the sibling contact's whole alarm set
    and the pump's motor-fault alarm stay clean — no alarm beyond the
    declared contract may annunciate."""
    sibling = [other for other in CONTACTS if other != kind][0]
    if not alarm_clear(snapshot, points, sibling, False):
        return False
    if flag(snapshot, points["fault"]) or flag(
        snapshot, points["fault_alarm"]
    ):
        return False
    return not any(
        flag(snapshot, points[f"fault_{key}"]) for key, _ in MANAGED
    )


def annunciation_record(snapshot, points, kind):
    """The declared annunciation the evidence carries — the contact's
    value and quality, the protection's own surfaces, and the alarm's
    two flags."""
    return {
        kind: {
            "contact": value(snapshot, points[kind]),
            "quality": quality_of(snapshot, points[kind]),
            "protect": value(snapshot, points["protect"]),
            "protect_ok": value(snapshot, points["protect_ok"]),
            "alarm": value(snapshot, points[f"{kind}_alarm"]),
            "unack": value(snapshot, points[f"{kind}_unack"]),
        }
    }


def cause_pass(args, tamper):
    """The quality-aware cause-alarm run: converge, hand-hold,
    degrade, recover, acknowledge, value-trip, stop, degrade, restore,
    audit. Returns `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the cause-alarm "
            "leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    with open(args.model) as handle:
        model = json.load(handle)
    points = signal_points(model)
    if points is None:
        raise Inconclusive(
            "the emitted model declares none of "
            + ", ".join(sorted(set(SEAMS.values())))
            + " — the leg has no declared quality-gated protection "
            "surface to degrade"
        )
    seam = quality_gated_seam(model, points)
    for kind in CONTACTS:
        # The guard's synthesized carrier — the alarm's condition
        # input, which the model declares no signal for.
        points[f"{kind}_alarm_in"] = seam[kind]["in"]
    receipted_seam(model, points)
    true, false = {"bool": True}, {"bool": False}
    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        plant_io = rig.plant_io
        # The field owner's writer claim: a release that claims at
        # startup reports the owner token on stderr ahead of its
        # listener; the leg's field writes join it so they land inside
        # the standing claim.
        claim = owner_token(rig.duty_preamble)

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

        # The declared wiring: the pinned release must serve the cause
        # guards the model wires the alarms through.
        schema = pair.get(f"{duty_url}/schema", "GET /schema", failures)
        if not contract_probe(schema, seam, points):
            raise Inconclusive(
                "the pinned release predates the quality-aware "
                "cause-alarm contract — its served registry reports no "
                "single-trip cause guard between the declared contacts "
                "and their managed alarms"
            )
        evidence["seam"] = seam

        # The shared claim this leg's field writes ride.
        join_claim(plant_io, claim, failures)

        # Phase 2 — the hand-held pump: manual mode and the operator's
        # hand request, both receipted, until the field's own stored
        # motor command reads energized — the running precondition every
        # contact's degradation interrupts.
        for key, want in (("hand", False), ("mode", True),
                          ("hand", True)):
            submit(duty_url, write_value(points[key], want), failures)
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: flag(snapshot, points["mode"])
            and flag(snapshot, points["hand"])
            and flag(snapshot, points["protect_ok"]),
            bound=START_BOUND,
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                "the declared pump never held its manual hand request — "
                f"mode reads {value(owner, points['mode'])}, hand "
                f"{value(owner, points['hand'])}"
            )
            raise Abort
        if not field_until(
                rig, failures, plant_io, points["cmd"], true,
                bound=START_BOUND):
            owner = tick(rig, failures)
            failures.append(
                "the field's motor command never read energized under "
                "the standing hand request — the leg's running "
                "precondition never arrived"
            )
            raise Abort
        owner = tick(rig, failures)
        evidence["hand_held"] = owner["tick"]
        digest_entries.append(
            {"phase": "hand-held", "tick": owner["tick"], "seam": seam}
        )

        drives = []

        def degrade(kind, ordinal):
            """Inject the declared bad-quality fault on one contact and
            prove the whole contract on it: the substituted quality
            over the standing stored value, the fail-safe trip, the
            named cause alarm's annunciation, nothing else announcing,
            and the field command cut."""
            standing = field_read(plant_io, points[kind])
            if standing != false:
                failures.append(
                    f"the field's {kind} contact does not read clear "
                    f"ahead of the drive — it reads {standing}"
                )
                raise Abort
            field_fault(
                plant_io, points[kind], BAD_FAULT, failures,
                "inject_fault"
            )
            owner = drive_until(
                rig,
                failures,
                lambda snapshot: flag(snapshot, points["protect"])
                and not flag(snapshot, points["protect_ok"])
                and alarm_standing(snapshot, points, kind, True)
                and nothing_else(snapshot, points, kind),
            )
            if owner is None:
                owner = tick(rig, failures)
                failures.append(
                    f"the degraded {kind} contact never annunciated its "
                    f"cause alarm — protect-tripped reads "
                    f"{value(owner, points['protect'])}, protections-ok "
                    f"{value(owner, points['protect_ok'])}, {kind}-alarm "
                    f"{value(owner, points[f'{kind}_alarm'])}, "
                    f"{kind}-unack {value(owner, points[f'{kind}_unack'])}"
                )
                raise Abort
            if quality_of(owner, points[kind]) != BAD_QUALITY \
                    or value(owner, points[kind]) != false:
                failures.append(
                    f"the degraded {kind} contact served "
                    f"{quality_of(owner, points[kind])} over "
                    f"{value(owner, points[kind])} — the substituted "
                    "reading must leave the standing field value"
                )
                raise Abort
            if proven(owner, points["avail"]):
                failures.append(
                    "the pumps' availability still reads proven under "
                    "the degraded contact — the fail-safe reading must "
                    "strip its leg"
                )
                raise Abort
            field_until(rig, failures, plant_io, points["cmd"], false)
            cut = field_read(plant_io, points["cmd"])
            # The doctored expectations: a leg requiring the field
            # command to stand through the trip, or requiring the cause
            # alarm to stay silent over the degraded contact, must name
            # the stop and the annunciation it saw.
            if tamper == "expect-standing" and ordinal == "thermal" \
                    and cut == false:
                failures.append(
                    "the protective stop de-energized the field command "
                    "under the degraded contact — the doctored leg "
                    "expected the field command to stand"
                )
                raise Abort
            if tamper == "expect-silent" and ordinal == "thermal":
                failures.append(
                    "the thermal cause alarm annunciated the degraded "
                    "contact — the doctored leg expected the cause "
                    "alarm to stay silent"
                )
                raise Abort
            if cut != false:
                failures.append(
                    "the field's motor command still reads energized "
                    "under the protective stop — the protection failed "
                    "to cut the field"
                )
                raise Abort
            drives.append(
                {"drive": ordinal, "contact": kind, "at": owner["tick"],
                 "field": cut}
            )
            return owner, annunciation_record(owner, points, kind)

        def acknowledge(kind):
            """The managed lifecycle's acknowledgment: the receipted
            press clears the standing latch on the consumed edge, then
            the released request re-arms the input."""
            press = write_value(points[f"{kind}_ack"], True)
            submit(duty_url, press, failures)
            owner = drive_until(
                rig,
                failures,
                lambda snapshot: not flag(
                    snapshot, points[f"{kind}_unack"]
                ),
            )
            if owner is None:
                failures.append(
                    f"the receipted {kind} ack never cleared the latch — "
                    "unacknowledged reads "
                    f"{value(tick(rig, failures), points[f'{kind}_unack'])}"
                )
                raise Abort
            release = write_value(points[f"{kind}_ack"], False)
            submit(duty_url, release, failures)
            owner = drive_until(
                rig,
                failures,
                lambda snapshot: not flag(snapshot, points[f"{kind}_ack"])
                and not flag(snapshot, points[f"{kind}_unack"]),
            )
            if owner is None:
                failures.append(
                    "the released ack request never re-armed the input"
                )
                raise Abort
            return press, release, owner

        def recover(kind, running):
            """Clear the injected fault and prove the declared
            recovery: the contact back at Good over its standing value,
            the trip clearing, the condition reporting clear while the
            latch stands, and — while the hand request stands — the
            field command returning."""
            field_fault(plant_io, points[kind], None, failures,
                        "clear_fault")
            owner = drive_until(
                rig,
                failures,
                lambda snapshot: quality_of(snapshot, points[kind])
                == "good"
                and not flag(snapshot, points["protect"])
                and flag(snapshot, points["protect_ok"])
                and alarm_clear(snapshot, points, kind, True),
            )
            if owner is None:
                owner = tick(rig, failures)
                failures.append(
                    f"the cleared {kind} contact left the alarm standing "
                    f"— contact quality "
                    f"{quality_of(owner, points[kind])}, protect "
                    f"{value(owner, points['protect'])}, alarm "
                    f"{value(owner, points[f'{kind}_alarm'])}"
                )
                raise Abort
            if running and not field_until(
                    rig, failures, plant_io, points["cmd"], true):
                failures.append(
                    "the standing hand request never re-energized the "
                    "field command after the protection cleared"
                )
                raise Abort
            press, release, owner = acknowledge(kind)
            return owner, press, release

        def ack_groups(kind):
            """The acknowledged pair's ordered record — the press's
            attributed settlement beside the latch's release, then the
            released request's own settlement."""
            return [
                [
                    ("settled", points[f"{kind}_ack"], TRUE, "applied",
                     ACTOR),
                    ("changed", points[f"{kind}_unack"], FALSE),
                    ("settled", points[f"{kind}_ack"], FALSE, "applied",
                     ACTOR),
                ]
            ]

        def degraded_groups(kind):
            """The degraded drive's ordered record — the substituted
            quality, the trip's own surfaces, and the annunciation it
            explains — then the cleared pair's return."""
            return [
                [
                    ("quality", points[kind], BAD_QUALITY),
                    ("changed", points["protect"], TRUE),
                    ("changed", points["protect_ok"], FALSE),
                    ("changed", points[f"{kind}_alarm"], TRUE),
                    ("changed", points[f"{kind}_unack"], TRUE),
                ],
                [
                    ("quality", points[kind], "good"),
                    ("changed", points["protect"], FALSE),
                    ("changed", points["protect_ok"], TRUE),
                    ("changed", points[f"{kind}_alarm"], FALSE),
                ],
            ]

        # Phase 3 — the degraded contacts on the running hand-held
        # pump, one per declared quality-gated protection contact.
        for kind in CONTACTS:
            owner, case = degrade(kind, kind)
            evidence[f"{kind}_degraded_at"] = owner["tick"]
            digest_entries.append({"phase": f"{kind}-degraded", **case})
            owner, press, release = recover(kind, True)
            evidence[f"{kind}_recovered_at"] = owner["tick"]
            digest_entries.append(
                {
                    "phase": f"{kind}-recovered",
                    "press": press,
                    "release": release,
                    "at": owner["tick"],
                }
            )

        # Phase 4 — the honest-absence half: a Good-quality value trip
        # annunciates identically, with no quality transition beside it
        # in the record.
        field_write(plant_io, points["thermal"], True, failures,
                    "the value write")
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: quality_of(snapshot, points["thermal"])
            == "good"
            and alarm_standing(snapshot, points, "thermal", True)
            and flag(snapshot, points["protect"]),
        )
        if owner is None:
            failures.append(
                "the Good-quality value trip on the thermal contact "
                "never annunciated its cause alarm — the quality-aware "
                "path must not rename a value trip"
            )
            raise Abort
        evidence["value_trip_at"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "value-trip",
                "at": owner["tick"],
                **annunciation_record(owner, points, "thermal"),
            }
        )
        field_write(plant_io, points["thermal"], False, failures,
                    "the release write")
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: alarm_clear(
                snapshot, points, "thermal", True
            )
            and not flag(snapshot, points["protect"]),
        )
        if owner is None:
            failures.append(
                "the released thermal contact left the alarm standing"
            )
            raise Abort
        press, release, owner = acknowledge("thermal")
        if not field_until(
                rig, failures, plant_io, points["cmd"], true):
            failures.append(
                "the standing hand request never re-energized the field "
                "command after the value trip cleared"
            )
            raise Abort
        owner = tick(rig, failures)
        evidence["value_cleared_at"] = owner["tick"]

        # Phase 5 — the stopped pump: the hand request releases while
        # the manual selection stands, so the auto leg stays gated and
        # the pump cannot start under any demand. A degraded contact
        # still annunciates its own cause alarm there, and plants
        # nothing beyond the declared contract.
        submit(duty_url, write_value(points["hand"], False), failures)
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: not flag(snapshot, points["hand"])
            and flag(snapshot, points["mode"])
            and not flag(snapshot, points["protect"]),
        )
        if owner is None:
            failures.append(
                "the hand request never released — the stopped-pump "
                "posture never arrived"
            )
            raise Abort
        if not field_until(
                rig, failures, plant_io, points["cmd"], false):
            failures.append(
                "the released hand request left the field's motor "
                "command energized"
            )
            raise Abort
        owner = tick(rig, failures)
        evidence["stopped_at"] = owner["tick"]
        owner, case = degrade("moisture", "stopped")
        if field_read(plant_io, points["cmd"]) != false:
            failures.append(
                "the stopped pump started under the degraded contact — "
                "no spurious start may follow the protective stop"
            )
            raise Abort
        evidence["stopped_degraded_at"] = owner["tick"]
        digest_entries.append({"phase": "stopped-degraded", **case})
        owner, press, release = recover("moisture", False)
        evidence["stopped_recovered_at"] = owner["tick"]

        # Phase 6 — the restore: the manual selection released, every
        # driven input back at its baseline.
        submit(duty_url, write_value(points["mode"], False), failures)
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: not flag(snapshot, points["mode"])
            and not flag(snapshot, points["hand"])
            and not flag(snapshot, points["protect"])
            and flag(snapshot, points["protect_ok"])
            and all(
                alarm_clear(snapshot, points, kind, False)
                for kind in CONTACTS
            ),
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                "the restored posture never landed — mode reads "
                f"{value(owner, points['mode'])}, hand "
                f"{value(owner, points['hand'])}"
            )
            raise Abort
        for kind in CONTACTS:
            if field_read(plant_io, points[kind]) != false:
                failures.append(
                    f"the leg left the {kind} contact standing"
                )
                raise Abort
        evidence["restored_at"] = owner["tick"]

        # Phase 7 — the record: the pair's roles never moved, both
        # peers' adopted receipt logs are identical, and the field
        # owner's durable journal carries each drive in run order.
        standby_role = pair.get(f"{standby_url}/role", "GET /role", failures)
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        sync = standby_role.get("sync")
        if standby_role.get("role") != "standby" or not (
            isinstance(sync, dict) and "tracking" in sync
        ):
            failures.append(
                "the standby's role moved through the quality-aware "
                f"cause-alarm drives — GET /role answers {standby_role}"
            )
        if duty_role.get("role") != "active":
            failures.append(
                "the field owner's role moved through the cause-alarm "
                f"drives — GET /role answers {duty_role}"
            )
        if failures:
            raise Abort
        receipts_duty = pair.get(f"{duty_url}/receipts", "GET /receipts",
                                 failures)
        receipts_standby = pair.get(
            f"{standby_url}/receipts", "GET /receipts", failures
        )
        if receipts_duty != receipts_standby:
            failures.append(
                "the peers' receipt logs diverged — the adopted audit "
                "is not one log"
            )
            raise Abort
        settled_writes = [
            command
            for command in (
                write_value(points["mode"], True),
                write_value(points["hand"], True),
                write_value(points["thermal_ack"], True),
                write_value(points["thermal_ack"], False),
                write_value(points["moisture_ack"], True),
                write_value(points["moisture_ack"], False),
                write_value(points["hand"], False),
                write_value(points["mode"], False),
            )
            if not settled(receipts_duty, command)
        ]
        if settled_writes:
            failures.append(
                "the leg's receipted writes never settled applied into "
                f"the adopted receipt log: {settled_writes}"
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
            for record_kind, record in pair.journal_records(journal_path)
            if record_kind == "entry"
        ]
        served = pair.get(f"{duty_url}/journal", "GET /journal", failures)
        events = journal_events(entries)
        if journal_events(served) != events:
            failures.append(
                "the served journal's transition stream diverges from "
                "the durable file's — the monitor does not answer the "
                "record it persists"
            )
            raise Abort
        for name, journal in (
            (duty_decl["name"], entries),
            (
                standby_decl["name"],
                pair.get(f"{standby_url}/journal", "GET /journal", failures),
            ),
        ):
            transitions = pair.role_transitions(journal)
            if transitions:
                failures.append(
                    f"{name}'s journal carries role changes "
                    f"{transitions} — a field fault must not move the "
                    "pair's roles"
                )
        groups = []
        for kind in CONTACTS:
            groups += degraded_groups(kind) + ack_groups(kind)
        groups.append(
            [
                ("changed", points["thermal"], TRUE),
                ("changed", points["protect"], TRUE),
                ("changed", points["thermal_alarm"], TRUE),
                ("changed", points["thermal_unack"], TRUE),
            ]
        )
        groups.append(
            [
                ("changed", points["thermal"], FALSE),
                ("changed", points["protect"], FALSE),
                ("changed", points["thermal_alarm"], FALSE),
            ]
        )
        groups += ack_groups("thermal")
        groups.append(
            [("settled", points["hand"], FALSE, "applied", ACTOR)]
        )
        groups += degraded_groups("moisture") + ack_groups("moisture")
        groups.append(
            [("settled", points["mode"], FALSE, "applied", ACTOR)]
        )
        failures.extend(ordered_group_misses(events, groups))
        for kind in CONTACTS:
            for key, _ in MANAGED:
                standing = [
                    event
                    for event in events
                    if event[0] == "changed"
                    and event[1] == points[f"{kind}_{key}"]
                    and event[2] == TRUE
                ]
                if standing:
                    failures.append(
                        f"the {kind} cause alarm's declared-quiet {key} "
                        f"stands in the record {standing} — the "
                        "annunciation lifecycle must never move it"
                    )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "audit",
                "duty_role": duty_role,
                "standby_role": standby_role,
                "drives": drives,
                "events": events,
            }
        )
        evidence["entries"] = len(entries)
    except Inconclusive as inconclusive:
        raise inconclusive
    except Abort as abort:
        failures.extend(str(argument) for argument in abort.args)
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
        choices=["expect-silent", "expect-standing"],
        help="doctor the leg's own expectation — the pass must fail "
        "naming the evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = cause_pass(args, args.tamper)
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            doctored = {
                "expect-silent": "the doctored expectation wanted the "
                "cause alarm to stay silent",
                "expect-standing": "the doctored expectation wanted the "
                "field command to stand",
            }[args.tamper]
            eprint(
                f"cause-alarm-quality: {doctored} — an inconclusive "
                "run offers the doctored case no evidence"
            )
            return 1
        eprint(f"cause-alarm-quality: inconclusive — {inconclusive}")
        print(f"cause-alarm-quality-digest inconclusive — {inconclusive}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"cause-alarm-quality: {line}")
        return 1
    for failure in failures:
        eprint(f"cause-alarm-quality: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"cause-alarm-quality: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored "
                "expectation"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"cause-alarm-quality-digest {digest} — tracking by tick "
        f"{evidence['converged']}, hand-held by tick "
        f"{evidence['hand_held']}, the degraded thermal annunciated at "
        f"tick {evidence['thermal_degraded_at']}, the degraded moisture "
        f"at tick {evidence['moisture_degraded_at']}, the value trip at "
        f"tick {evidence['value_trip_at']}, the stopped pump degraded at "
        f"tick {evidence['stopped_degraded_at']}, restored at tick "
        f"{evidence['restored_at']}, {evidence['entries']} journal "
        "entries"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())