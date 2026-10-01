#!/usr/bin/env python3
"""The pair contract's consumed-edge acknowledgment lifecycle leg —
WW-ALM-002's lifecycle clause exercised end to end on the deployed
consumer pair: the consumer-boundary mirror of the qa-rig leg at
qa_lane/scenarios/3950_ack_edge_lifecycle.py, covering the
#781/#947/#961 contract family on the released images.

ci/check.sh runs this script twice against the same declared
deployment as ci/legs/pair.py — `dcs-plant-server` serving the emitted
model and dynamics, plus the two manifest-declared `dcs-controller
--driven --remote` peers, the standby wired at the field owner's
monitor. With the pair converged and tracking, the leg drives the
`p101-moisture` alarm's consumed-edge lifecycle — the field contact
wired through the pump's moisture-cause guard interlock into the
managed alarm's `in` and into nothing else on the control path:

- the trip: a field-side `write` stands the journaled contact, the
  guard trips, `alarm` asserts and `unacknowledged` latches — the
  journaled `point_changed` record;
- the consumed press: a receipted `write_value` on the writable
  `ack` point settles `applied` under the leg's actor — the
  false→true edge consumed exactly once, `unacknowledged` clearing
  while `alarm` keeps reporting the standing condition, the input
  itself held `true` because the release write has not gone out;
- the held re-latch: with the level still held, clearing and
  re-driving the contact re-latches `unacknowledged` — a held level
  is not an acknowledgment (#781);
- the promotion: the pair switches with the latch standing and the
  ack input held — every handover scan must leave the latch
  standing, because the promoted run inheriting the checkpointed
  edge state sees a held level, not a fresh edge (#947/#961). The
  new active's served checkpoint carries the adopted `ack` and
  `unacknowledged` states — the run-level proof the edge rode;
- the dropped release: the owed `ack=false` write submitted to the
  demoted peer, refused `not_active` at its role boundary and
  journaled there as a rejected settlement — the mid-flight
  failure. The pending release stays armed: the input still reads
  `true`, the latch still stands;
- the held press: a second `ack=true` write on the held level
  settles `applied` and journals as a no-op — no edge, nothing
  consumed, the latch standing;
- the recovery: the late release re-arms the edge and a fresh press
  acknowledges — a dropped release cannot wedge later
  acknowledgments;
- the restore: the acknowledged state rides the switch back to the
  manifest's declared roles, the contact and the ack input return
  to baseline, every journaled transition lands in run order beside
  the attributed settlements, the served journal answering the
  durable file's record on both peers.

A pinned release predating the consumed-edge contract — one whose
checkpointed alarm state carries no `ack` edge marker — cannot run
this lifecycle; the leg reports `inconclusive`, never a product
failure. A contract-present release that re-consumes the stale edge
across the boundary, loses the armed release, or wedges a later
acknowledgment fails by name.

On success one `ack-edge-lifecycle-digest <sha256>` line prints —
the check runs two passes and compares them. Every assertion failure
collects onto stderr prefixed `ack-edge-lifecycle:` and exits 1 —
the check names it ack-edge-failed; differing digests name
ack-edge-lifecycle-nondeterministic.

`--tamper` doctors the leg's own expectation so a doctored
implementation — a stale edge re-consumed across the promotion, or
a held press clearing the latch — would pass while the honest run
reports the named failure:

- `expect-reconsumed` asserts the journal carries a third
  `unacknowledged` clear — the phantom consume the contract
  forbids — the honest record of two fails it;
- `expect-cleared` asserts the held press cleared the latch — the
  honest no-op leaving it standing fails it.

Both exit 1 like any failure: the check asserts each reports the
named diagnostic rather than passing silently.
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
# The doctored cases: a leg asserting the stale held edge
# re-consumed across the promotion — a third unacknowledged clear
# the honest journal never carries — and a leg asserting the held
# press cleared the latch — the no-op clearing nothing — must each
# surface the named diagnostic rather than passing silently.
LEG = {
    "order": 280,
    "title": "the consumed-edge acknowledgment lifecycle leg",
    "passes": "ack-edge-lifecycle",
    "failed": "ack-edge-failed",
    "tampers": [
        {
            "name": "expect-reconsumed",
            "passed": "a expect-reconsumed case passed the ack-edge-lifecycle leg",
            "missed": "the expect-reconsumed case did not report its named diagnostic",
            "evidence": ["the doctored expectation wanted a re-consumed edge"],
        },
        {
            "name": "expect-cleared",
            "passed": "a expect-cleared case passed the ack-edge-lifecycle leg",
            "missed": "the expect-cleared case did not report its named diagnostic",
            "evidence": ["the doctored expectation wanted the held press to clear"],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The pinned release predates the contract the leg exercises —
    the run classifies inconclusive, never a product failure."""


# The bound each driven phase gets to land its declared effect across
# the wiring's one-scan carrier crossings. The actor the leg's
# receipted submissions declare — the attribution every settled
# receipt, applied or refused, must carry. The held window's scan
# count — the driven ticks the latch must stand through once the
# held press settles, the wedge the release owed.
SETTLE_BOUND = 16
HELD_TICKS = 4
ACTOR = "ci-ack-edge"

# The signal names resolving the leg's point ids out of the emitted
# model — the same names the signal index serves, so the leg
# exercises the declared seam, never a hard-coded id. The moisture
# contact is the rig's declared lever for this alarm: journaled,
# field-driven, and wired through the pump's moisture-cause guard
# into the managed alarm's `in` and into nothing else on the
# control path.
SIGNALS = {
    "contact": "p101-moisture",
    "ack": "p101-moisture-ack",
    "alarm": "p101-moisture-alarm",
    "unack": "p101-moisture-unacknowledged",
    "shelved": "p101-moisture-shelved",
    "suppressed": "p101-moisture-suppressed",
    "oos": "p101-moisture-out-of-service",
}

# The component kinds whose `in`/`ack`/`alarm`/`unacknowledged`
# vocabulary carries the consumed-edge contract — the descriptor
# kinds an `ack` input binds on.
MANAGED_KINDS = ("managed-bool-latching-alarm", "managed-latching-alarm")


def signal_points(model):
    """The `{key: point id}` map the emitted model's signal index
    declares for the leg's lifecycle — None when the model declares
    no such surface. A signal's `source` is the point it names; the
    lowest-signal-id-wins rule the served index applies. The contact
    must be a journaled field `In`, the ack a writable internal `In`
    — the seams the lifecycle drives and commands."""
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
    contact = declared.get(points["contact"], {})
    if (
        contact.get("direction") != "in"
        or contact.get("value_type") != "bool"
        or contact.get("channel") is None
        or not contact.get("journaled")
    ):
        return None
    ack = declared.get(points["ack"], {})
    if (
        ack.get("direction") != "in"
        or ack.get("value_type") != "bool"
        or not ack.get("writable")
    ):
        return None
    return points


def alarm_component(model, points):
    """The managed-alarm instance the leg's lifecycle rides — the
    `<kind>:<id>` name the checkpoint's `components` map keys — or
    None when the emitted model binds no managed alarm's `alarm`
    output to the declared point, or binds its `in`/`ack` ports off
    the declared seam. Resolved from the artifact's wiring, never
    the name convention."""
    kinds = {
        component["id"]: component["kind"]
        for component in model.get("components", [])
        if component.get("kind") in MANAGED_KINDS
    }
    owners = {}
    bound = {}
    for connection in model.get("connections", []):
        port = connection.get("from", {}).get("port", {})
        if (
            port.get("name") == "alarm"
            and connection.get("to", {}).get("point") == points["alarm"]
        ):
            owners[port.get("component")] = True
        port = connection.get("to", {}).get("port", {})
        if not port:
            continue
        source = connection.get("from", {})
        if "point" in source:
            bound.setdefault(port.get("component"), {})[
                port.get("name")
            ] = source["point"]
        elif "port" in source:
            bound.setdefault(port.get("component"), {}).setdefault(
                port.get("name"), True
            )
    for ident in owners:
        if ident not in kinds:
            continue
        ports = bound.get(ident, {})
        if (
            ports.get("ack") == points["ack"]
            and ports.get("in") is not None
            and ports.get("suppress") is None
            and ports.get("oos") is None
        ):
            return f"{kinds[ident]}:{ident}"
    return None


def contract_probe(checkpoint, component):
    """The consumed-edge contract marker the pinned release must
    carry: the alarm's checkpointed state naming `ack` — the edge
    tracking the #947/#961 family added so a stale held level
    cannot re-consume across a promotion. True when the served
    checkpoint carries the marker, False when the release predates
    it."""
    element = (checkpoint.get("components") or {}).get(component) or {}
    state = element.get("fields") or element
    return isinstance(state, dict) and "ack" in state


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
    """The adopted receipt log's applied entries for `command`, in
    submission order — identical submissions match positionally."""
    return [
        entry
        for entry in receipts
        if entry.get("command") == command
        and simulate.receipt_outcome(entry) == "applied"
    ]


def field_write(plant_io, owner_token, point, boolean, failures):
    """One field-side `write` through the plant protocol — the same
    seam the scenario legs drive. Where the release records the
    field owner's writer claim (the duty's reported `owner token`),
    this attachment joins it first — `ensure_writer` under the same
    token — so the write lands inside the standing claim; where no
    claim stands the write lands unfenced."""
    if owner_token is not None:
        verdict = plant_io.request(
            {"op": "ensure_writer", "owner": owner_token}
        )
        if verdict.get("result") not in ("done", "claimed_shared"):
            failures.append(
                f"the writer-claim join under the recorded owner "
                f"token answered {verdict}"
            )
            raise Abort
    verdict = plant_io.request(
        {"op": "write", "point": point, "value": {"bool": boolean}}
    )
    if verdict.get("result") != "done":
        failures.append(
            f"the field-side write on the contact answered {verdict}"
        )
        raise Abort


def owner_token(preamble):
    """The field-ownership token a launched active's claim line
    reports — `field write-ownership claim held under owner token N`
    ahead of the listener where the release records it; None where
    the release line claims only on promotion and the field stands
    unclaimed until then."""
    for line in preamble:
        claimed = re.search(r"owner token (\d+)", line)
        if claimed:
            return int(claimed.group(1))
    return None


def lifecycle(entries, ack_point, watched):
    """The leg's audit stream out of a journal entry list, in `seq`
    order: `{"kind": "settled", "seq", "tick", "to", "outcome",
    "applied", "actor"}` for each `command_settled` receipt naming
    the ack point — `to` the written value, `applied` the applied
    outcome's scan tick or None — and `{"kind": "changed", "seq",
    "tick", "point", "to"}` for each `point_changed` on the watched
    journaled points."""
    events = []
    for entry in entries:
        event = entry.get("event", {})
        if "command_settled" in event:
            receipt = event["command_settled"].get("receipt", {})
            write = receipt.get("command", {}).get("write_value", {})
            if write.get("point") != ack_point:
                continue
            events.append(
                {
                    "kind": "settled",
                    "seq": entry.get("seq"),
                    "tick": entry.get("tick"),
                    "to": write.get("value"),
                    "outcome": simulate.receipt_outcome(receipt),
                    "applied": (
                        (receipt.get("outcome") or {}).get("applied") or {}
                    ).get("tick"),
                    "actor": receipt.get("actor"),
                }
            )
        elif "point_changed" in event:
            change = event["point_changed"]
            if change.get("point") in watched:
                events.append(
                    {
                        "kind": "changed",
                        "seq": entry.get("seq"),
                        "tick": entry.get("tick"),
                        "point": change.get("point"),
                        "from": change.get("from"),
                        "to": change.get("to"),
                    }
                )
    return events


def ordered_group_misses(events, groups):
    """The ordered-audit check over the leg's marker tuples: each
    group's markers must appear in the record's `seq` order after
    the previous group's — the leg's lifecycle landing in run order.
    Markers are `("settled", to, outcome)` or `("changed", point,
    to)` — `to` the written or transitioned value. Returns the named
    misses."""
    def marker(event):
        if event["kind"] == "settled":
            return ("settled", event["to"], event["outcome"])
        return ("changed", event["point"], event["to"])

    failures = []
    cursor = 0
    for index, group in enumerate(groups):
        positions = []
        for want in group:
            position = next(
                (
                    at
                    for at in range(cursor, len(events))
                    if marker(events[at]) == want
                ),
                None,
            )
            if position is None:
                failures.append(
                    f"the journal carries no {want} at or after "
                    f"group {index}'s position — the lifecycle's "
                    "transition is missing or out of order"
                )
            else:
                positions.append(position)
        if positions:
            cursor = max(positions) + 1
    return failures


def journal_entries(files):
    """A peer's durable journal entries — `entry` records only, run
    boundaries excluded."""
    path = files.get("journal_file")
    if path is None or not os.path.exists(path):
        raise Abort(
            "a peer's declared journal file does not exist — the "
            "--journal-file flag was not honored"
        )
    return [
        record
        for kind, record in pair.journal_records(path)
        if kind == "entry"
    ]


def ack_edge_pass(args, tamper):
    """The consumed-edge run: converge, probe, trip, press, re-latch,
    promote, drop, hold, release, recover, restore, audit. Returns
    `(digest_entries, evidence, failures)`; raises Inconclusive
    when the pinned release predates the contract the leg
    exercises."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "ack-edge-lifecycle leg has nothing to exercise"
        )
    with open(args.model) as handle:
        model = json.load(handle)
    points = signal_points(model)
    if points is None:
        raise Inconclusive(
            "the emitted model declares no p101-moisture alarm "
            "acknowledgment seam — the deployed surface predates the "
            "lifecycle the leg exercises"
        )
    component = alarm_component(model, points)
    if component is None:
        raise Inconclusive(
            "no managed alarm binds the declared alarm/ack seam — "
            "the deployed surface predates the managed lifecycle"
        )
    true, false = {"bool": True}, {"bool": False}
    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        plant_io = rig.plant_io
        # The field owner's writer claim: a release that claims at
        # startup reports the owner token on stderr ahead of its
        # listener; the earlier release line claims only on
        # promotion, so no token means the field stands unclaimed
        # and the leg's field writes land unfenced. The token is a
        # process identity — the same token answers across the
        # pair's demote/promote rounds.
        token = owner_token(rig.duty_preamble)

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
        for key in ("contact", "ack", "alarm", "unack",
                    "shelved", "suppressed", "oos"):
            if value(owner, points[key]) != false:
                failures.append(
                    f"the converged pair is not quiet — {key} reads "
                    f"{value(owner, points[key])} before the leg drives"
                )
        if failures:
            raise Abort

        # Phase 2 — the contract probe: the served checkpoint must
        # carry the consumed-edge marker — the alarm's checkpointed
        # `ack` edge tracking — before the leg can run the lifecycle.
        # A release predating it would re-consume the stale held edge
        # across the promotion; the run reports inconclusive, never a
        # product failure.
        checkpoint = pair.get(
            f"{duty_url}/checkpoint", "GET /checkpoint", failures
        )
        if not contract_probe(checkpoint, component):
            raise Inconclusive(
                f"the served checkpoint carries no `ack` edge state "
                f"for {component} — the pinned release predates the "
                "consumed-edge contract"
            )
        digest_entries.append(
            {"phase": "contract", "component": component,
             "consumed_edge": True}
        )

        # Phase 3 — the trip: the field-side write stands the
        # journaled contact, the guard trips, and `alarm`/
        # `unacknowledged` annunciate on the owner's monitor.
        field_write(plant_io, token, points["contact"], True, failures)
        owner = drive_until(
            rig, standby_url, duty_url, failures,
            lambda snapshot: value(snapshot, points["contact"]) == true
            and value(snapshot, points["alarm"]) == true
            and value(snapshot, points["unack"]) == true,
        )
        if owner is None:
            owner = tick(rig, standby_url, duty_url, failures)
            failures.append(
                "the driven contact never annunciated — alarm reads "
                f"{value(owner, points['alarm'])}, unacknowledged "
                f"{value(owner, points['unack'])}"
            )
            raise Abort
        if value(owner, points["ack"]) != false:
            failures.append(
                "the ack input stands ahead of the press — the "
                "lifecycle needs the input re-armed"
            )
            raise Abort
        evidence["tripped_at"] = owner["tick"]
        digest_entries.append(
            {"phase": "trip", "tripped_at": owner["tick"]}
        )

        # Phase 4 — the consumed press: the receipted `ack` write is
        # the operator's press — the false→true edge consumed exactly
        # once, the latch clearing while `alarm` keeps reporting the
        # standing condition. The release write never goes out — the
        # input serves held `true`.
        press = write_value(points["ack"], True)
        submit(duty_url, press, failures)
        owner = drive_until(
            rig, standby_url, duty_url, failures,
            lambda snapshot: value(snapshot, points["unack"]) == false,
        )
        if owner is None:
            owner = tick(rig, standby_url, duty_url, failures)
            failures.append(
                "the receipted press never cleared the latch — "
                f"unacknowledged reads "
                f"{value(owner, points['unack'])}"
            )
            raise Abort
        if (
            value(owner, points["alarm"]) != true
            or value(owner, points["ack"]) != true
        ):
            failures.append(
                "the consumed press disturbed the standing state — "
                f"alarm reads {value(owner, points['alarm'])}, the "
                f"held ack input {value(owner, points['ack'])}"
            )
            raise Abort
        receipts = pair.get(f"{duty_url}/receipts", "GET /receipts", failures)
        if len(settled(receipts, press)) != 1:
            failures.append(
                "the press did not settle applied into the adopted "
                "receipt log exactly once"
            )
            raise Abort
        evidence["consumed_at"] = owner["tick"]
        digest_entries.append(
            {"phase": "press", "consumed_at": owner["tick"]}
        )

        # Phase 5 — the held re-latch: with the held level standing,
        # clearing the contact drops the standing alarm while the
        # acknowledged latch stays down, and re-driving the contact
        # re-latches `unacknowledged` — a held level is not an
        # acknowledgment.
        field_write(plant_io, token, points["contact"], False, failures)
        owner = drive_until(
            rig, standby_url, duty_url, failures,
            lambda snapshot: value(snapshot, points["alarm"]) == false
            and value(snapshot, points["unack"]) == false,
        )
        if owner is None:
            owner = tick(rig, standby_url, duty_url, failures)
            failures.append(
                "the cleared contact left the alarm standing — "
                f"alarm reads {value(owner, points['alarm'])}, "
                f"unacknowledged {value(owner, points['unack'])}"
            )
            raise Abort
        if value(owner, points["ack"]) != true:
            failures.append(
                "the held ack input dropped — the release the leg "
                f"never sent landed anyway: "
                f"{value(owner, points['ack'])}"
            )
            raise Abort
        field_write(plant_io, token, points["contact"], True, failures)
        owner = drive_until(
            rig, standby_url, duty_url, failures,
            lambda snapshot: value(snapshot, points["alarm"]) == true
            and value(snapshot, points["unack"]) == true,
        )
        if owner is None:
            owner = tick(rig, standby_url, duty_url, failures)
            failures.append(
                "the fresh trip never re-latched under the held "
                f"level — alarm reads {value(owner, points['alarm'])}, "
                f"unacknowledged {value(owner, points['unack'])} — a "
                "held level must not silently acknowledge"
            )
            raise Abort
        evidence["relatched_at"] = owner["tick"]
        digest_entries.append(
            {"phase": "relatch", "relatched_at": owner["tick"]}
        )

        # Phase 6 — the promotion: the pair switches with the latch
        # standing and the ack input held. The promoted run's
        # checkpointed `ack` edge state is the contract's claim that
        # the held level is not a fresh edge — every handover scan
        # must leave the latch standing, and the new active's served
        # checkpoint must carry the adopted states.
        def held_across():
            promoted = pair.get(
                f"{standby_url}/snapshot", "GET /snapshot", failures
            )
            if value(promoted, points["unack"]) != true:
                failures.append(
                    "the latch dropped across the promotion — the "
                    "promoted run re-consumed the stale held edge"
                )
                raise Abort

        switch = rig.switch(
            duty_url,
            standby_url,
            failures,
            after_tick=held_across,
        )
        owner = switch["owner"]
        if (
            value(owner, points["ack"]) != true
            or value(owner, points["alarm"]) != true
            or value(owner, points["unack"]) != true
        ):
            failures.append(
                "the promoted run did not inherit the held "
                f"lifecycle — ack reads {value(owner, points['ack'])}, "
                f"alarm {value(owner, points['alarm'])}, "
                f"unacknowledged {value(owner, points['unack'])}"
            )
            raise Abort
        checkpoint = pair.get(
            f"{standby_url}/checkpoint", "GET /checkpoint", failures
        )
        element = (checkpoint.get("components") or {}).get(component) or {}
        state = element.get("fields") or element
        if state.get("ack") != true or state.get("unacknowledged") != true:
            failures.append(
                "the promoted run's checkpoint does not carry the "
                f"adopted edge state — {component} reads {state}"
            )
            raise Abort
        evidence["promoted_at"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "promote",
                "ticks": switch["ticks"],
                "ack_held": True,
                "unack_standing": True,
            }
        )

        # Phase 7 — the dropped release: the owed `ack=false` write
        # submitted to the demoted peer is refused `not_active` at its
        # role boundary — the mid-flight failure the pending release
        # must survive. The held input does not clear and the latch
        # stands.
        release = write_value(points["ack"], False)
        status, refusal = pair.request(
            f"{duty_url}/command",
            {"command": release, "actor": ACTOR},
        )
        reason = (
            refusal.get("outcome", {}).get("rejected", {}).get("reason", {})
            if isinstance(refusal, dict)
            else {}
        )
        if status != 200 or "not_active" not in reason:
            failures.append(
                f"the release on the demoted peer answered {status} "
                f"{refusal}, expected a rejected not_active receipt — "
                "the mid-flight failure never failed"
            )
            raise Abort
        owner = None
        for _ in range(HELD_TICKS):
            owner = tick(rig, duty_url, standby_url, failures)
            if (
                value(owner, points["ack"]) != true
                or value(owner, points["unack"]) != true
            ):
                failures.append(
                    "the dropped release cleared anyway — the pending "
                    f"release was not armed: ack reads "
                    f"{value(owner, points['ack'])}, unacknowledged "
                    f"{value(owner, points['unack'])}"
                )
                raise Abort
        evidence["drop_held_at"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "dropped-release",
                "refusal": "not_active",
                "held_through": owner["tick"],
            }
        )

        # Phase 8 — the held press: a second `ack=true` write on the
        # still-held level settles `applied` and journals as a no-op
        # — the input never fell, so no edge exists to consume and
        # the latch stands.
        submit(standby_url, press, failures)
        owner = tick(rig, duty_url, standby_url, failures)
        receipts = pair.get(
            f"{standby_url}/receipts", "GET /receipts", failures
        )
        if len(settled(receipts, press)) != 2:
            failures.append(
                "the held press did not settle applied into the "
                "adopted receipt log — the journaled no-op is "
                "missing"
            )
            raise Abort
        for _ in range(HELD_TICKS):
            owner = tick(rig, duty_url, standby_url, failures)
        if tamper == "expect-cleared":
            if value(owner, points["unack"]) != false:
                failures.append(
                    "the doctored expectation wanted the held press "
                    f"to clear the latch — unacknowledged still reads "
                    f"{value(owner, points['unack'])}"
                )
                raise Abort
        elif value(owner, points["unack"]) != true:
            failures.append(
                "the held press cleared the latch — the journaled "
                f"no-op re-consumed: unacknowledged reads "
                f"{value(owner, points['unack'])}"
            )
            raise Abort
        evidence["held_press_at"] = owner["tick"]
        digest_entries.append(
            {"phase": "held-press", "held_at": owner["tick"]}
        )

        # Phase 9 — the late release: the owed `ack=false` finally
        # lands on the new active — a falling write acknowledges
        # nothing, so the latch stands.
        submit(standby_url, release, failures)
        owner = drive_until(
            rig, duty_url, standby_url, failures,
            lambda snapshot: value(snapshot, points["ack"]) == false,
        )
        if owner is None:
            owner = tick(rig, duty_url, standby_url, failures)
            failures.append(
                "the late release never landed — the ack input "
                f"still reads {value(owner, points['ack'])}"
            )
            raise Abort
        if value(owner, points["unack"]) != true:
            failures.append(
                "the release cleared the latch — a falling write "
                "acknowledges nothing: unacknowledged reads "
                f"{value(owner, points['unack'])}"
            )
            raise Abort
        evidence["released_at"] = owner["tick"]
        digest_entries.append(
            {"phase": "release", "released_at": owner["tick"]}
        )

        # Phase 10 — the recovered press: a fresh `ack=true` write on
        # the re-armed input is a real rising edge — the latch clears,
        # proving the dropped release wedged no later acknowledgment.
        submit(standby_url, press, failures)
        owner = drive_until(
            rig, duty_url, standby_url, failures,
            lambda snapshot: value(snapshot, points["unack"]) == false,
        )
        if owner is None:
            owner = tick(rig, duty_url, standby_url, failures)
            failures.append(
                "the recovered press never cleared the latch — "
                f"unacknowledged reads "
                f"{value(owner, points['unack'])}"
            )
            raise Abort
        receipts = pair.get(
            f"{standby_url}/receipts", "GET /receipts", failures
        )
        if len(settled(receipts, press)) != 3:
            failures.append(
                "the recovered press did not settle applied into the "
                "adopted receipt log"
            )
            raise Abort
        evidence["recovered_at"] = owner["tick"]
        digest_entries.append(
            {"phase": "recovered", "consumed_at": owner["tick"]}
        )

        # Phase 11 — the restore: the acknowledged state rides the
        # switch back to the manifest's declared roles — the ack
        # input still held, the latch now clear. Then the field
        # contact and the ack input return to baseline through the
        # same seams that drove them.
        switch = rig.switch(
            standby_url,
            duty_url,
            failures,
        )
        owner = switch["owner"]
        checkpoint = pair.get(
            f"{duty_url}/checkpoint", "GET /checkpoint", failures
        )
        element = (checkpoint.get("components") or {}).get(component) or {}
        state = element.get("fields") or element
        if (
            value(owner, points["ack"]) != true
            or state.get("ack") != true
            or state.get("unacknowledged") != false
        ):
            failures.append(
                "the acknowledged state did not ride the second "
                f"promotion — the restored checkpoint's {component} "
                f"reads {state}, the ack input "
                f"{value(owner, points['ack'])}"
            )
            raise Abort
        field_write(plant_io, token, points["contact"], False, failures)
        owner = drive_until(
            rig, standby_url, duty_url, failures,
            lambda snapshot: value(snapshot, points["alarm"]) == false,
        )
        if owner is None:
            owner = tick(rig, standby_url, duty_url, failures)
            failures.append(
                "the cleared contact left the alarm standing at "
                f"restore — alarm reads {value(owner, points['alarm'])}"
            )
            raise Abort
        submit(duty_url, release, failures)
        owner = drive_until(
            rig, standby_url, duty_url, failures,
            lambda snapshot: value(snapshot, points["ack"]) == false,
        )
        if owner is None:
            owner = tick(rig, standby_url, duty_url, failures)
            failures.append(
                "the restore release never landed — the ack input "
                f"still reads {value(owner, points['ack'])}"
            )
            raise Abort
        for key in ("contact", "ack", "alarm", "unack",
                    "shelved", "suppressed", "oos"):
            if value(owner, points[key]) != false:
                failures.append(
                    f"the leg left {key} standing — it reads "
                    f"{value(owner, points[key])} at restore"
                )
        if failures:
            raise Abort
        evidence["restored_at"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "restore",
                "ticks": switch["ticks"],
                "restored_at": owner["tick"],
            }
        )

        # Phase 12 — the audit: each peer's durable journal and its
        # served journal must answer the same lifecycle — the
        # journaled transitions in declared order, the press settling
        # applied beside the clear it consumed, the held press
        # settling applied beside nothing, the demoted peer carrying
        # the refused release its boundary journaled, and the peers'
        # adopted receipt logs reading as one audit trail.
        watched = (points["contact"], points["alarm"], points["unack"])
        peers = {
            rig.duty_decl["name"]: (
                rig.duty_files, f"{duty_url}/journal"
            ),
            rig.standby_decl["name"]: (
                rig.standby_files, f"{standby_url}/journal"
            ),
        }
        streams = {}
        for name, (files, url) in peers.items():
            entries = journal_entries(files)
            seqs = [entry["seq"] for entry in entries]
            if seqs != list(range(1, len(seqs) + 1)):
                failures.append(
                    f"{name}'s journal seqs are not 1..n in order: "
                    f"{seqs}"
                )
                raise Abort
            served = pair.get(url, "GET /journal", failures)
            recorded = lifecycle(entries, points["ack"], watched)
            if recorded != lifecycle(served, points["ack"], watched):
                failures.append(
                    f"{name}'s served journal's lifecycle diverges "
                    "from the durable file's — the monitor does not "
                    "answer the record it persists"
                )
                raise Abort
            streams[name] = recorded
        groups = [
            [("changed", points["contact"], true)],
            [
                ("changed", points["alarm"], true),
                ("changed", points["unack"], true),
            ],
            [
                ("settled", true, "applied"),
                ("changed", points["unack"], false),
            ],
            [("changed", points["contact"], false)],
            [("changed", points["alarm"], false)],
            [("changed", points["contact"], true)],
            [
                ("changed", points["alarm"], true),
                ("changed", points["unack"], true),
            ],
            [("settled", true, "applied")],
            [("settled", false, "applied")],
            [
                ("settled", true, "applied"),
                ("changed", points["unack"], false),
            ],
            [("changed", points["contact"], false)],
            [("changed", points["alarm"], false)],
            [("settled", false, "applied")],
        ]
        audits = {}
        for name, stream in streams.items():
            misses = ordered_group_misses(stream, groups)
            if misses:
                failures.extend(
                    f"{name}'s journal: {miss}" for miss in misses
                )
                raise Abort
            settles = [
                event for event in stream if event["kind"] == "settled"
            ]
            presses = [
                event
                for event in settles
                if event["to"] == true and event["outcome"] == "applied"
            ]
            releases = [
                event
                for event in settles
                if event["to"] == false and event["outcome"] == "applied"
            ]
            rejected = [
                event for event in settles if event["outcome"] != "applied"
            ]
            if len(presses) != 3 or len(releases) != 2:
                failures.append(
                    f"{name}'s journal carries {len(presses)} applied "
                    f"presses and {len(releases)} applied releases on "
                    "the ack point — the lifecycle's settlement "
                    "record is not the run's"
                )
                raise Abort
            clears = [
                event
                for event in stream
                if event["kind"] == "changed"
                and event["point"] == points["unack"]
            ]
            clears_to = [event["to"] for event in clears]
            if clears_to != [false, true, false, true, false]:
                failures.append(
                    f"{name}'s journal carries the unacknowledged "
                    f"transitions {clears_to} — the consumed-edge "
                    "lifecycle is not the run's"
                )
                raise Abort
            audits[name] = {
                "settles": settles,
                "clears": clears,
                "rejected": rejected,
            }
        # The consumed-edge pairings on the owner whose scan settled
        # each press: the duty journaled the first consume beside
        # its receipt, the promoted peer the recovered one — and no
        # journal anywhere carries a clear pairing the held press.
        duty_audit = audits[rig.duty_decl["name"]]
        standby_audit = audits[rig.standby_decl["name"]]
        consume = lambda event: (
            event["to"] == false and event.get("from") == true
        )
        press_ticks = [
            event["applied"] for event in duty_audit["settles"]
            if event["to"] == true and event["outcome"] == "applied"
        ]
        first_clear = next(
            event for event in duty_audit["clears"] if consume(event)
        )
        if first_clear["tick"] != press_ticks[0]:
            failures.append(
                "the first consume's transition journaled at tick "
                f"{first_clear['tick']} — the press settled at "
                f"{press_ticks[0]}, so the clear pairs no receipted "
                "press"
            )
            raise Abort
        promoted_press_ticks = [
            event["applied"] for event in standby_audit["settles"]
            if event["to"] == true and event["outcome"] == "applied"
        ]
        last_clear = [
            event for event in standby_audit["clears"] if consume(event)
        ][-1]
        if last_clear["tick"] != promoted_press_ticks[-1]:
            failures.append(
                "the recovered consume's transition journaled at "
                f"tick {last_clear['tick']} — the press settled at "
                f"{promoted_press_ticks[-1]}, so the clear pairs no "
                "receipted press"
            )
            raise Abort
        held_settle = standby_audit["settles"][
            next(
                at
                for at, event in enumerate(standby_audit["settles"])
                if event["to"] == true and event["outcome"] == "applied"
                and at > 0
            )
        ]
        if tamper == "expect-reconsumed":
            consumed = [
                event for event in standby_audit["clears"] if consume(event)
            ]
            if len(consumed) < 3:
                failures.append(
                    "the doctored expectation wanted a re-consumed "
                    f"edge — the journal carries {len(consumed)} "
                    "unacknowledged clears, not the three the "
                    "doctored lifecycle needs"
                )
                raise Abort
        paired = [
            event
            for event in standby_audit["clears"]
            if consume(event) and event["tick"] == held_settle["applied"]
        ]
        if paired:
            failures.append(
                f"an unacknowledged clear pairs the held press's "
                f"settle at tick {held_settle['applied']} — the "
                "journaled no-op re-consumed"
            )
            raise Abort
        refusals = {
            name: len(audit["rejected"]) for name, audit in audits.items()
        }
        if refusals[rig.duty_decl["name"]] != 1:
            failures.append(
                "the demoted peer's journal does not carry the "
                "release its role boundary refused — the dropped "
                "write is unaudited"
            )
            raise Abort
        if refusals[rig.standby_decl["name"]] != 0:
            failures.append(
                "the field owner's journal carries a refused settle "
                "the boundary never produced"
            )
            raise Abort
        receipts_duty = pair.get(f"{duty_url}/receipts", "GET /receipts", failures)
        receipts_standby = pair.get(
            f"{standby_url}/receipts", "GET /receipts", failures
        )
        if receipts_duty != receipts_standby:
            failures.append(
                "the peers' receipt logs diverged — the adopted "
                "audit trail is not identical on both peers"
            )
            raise Abort
        evidence["entries"] = sum(
            len(streams[name]) for name in streams
        )
        digest_entries.append(
            {
                "phase": "audit",
                "settles": {
                    name: len(audit["settles"])
                    for name, audit in audits.items()
                },
                "clears": {
                    name: len(audit["clears"])
                    for name, audit in audits.items()
                },
                "rejected": refusals,
            }
        )
    except Inconclusive:
        raise
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        if rig is not None:
            rig.close()
    return digest_entries, evidence, failures


def tick(rig, tracked_url, owner_url, failures):
    """One driven pair tick — the tracking peer scanned first so its
    pull applies the owner's latest checkpoint, then the field owner —
    asserting the peers' images stay identical. Returns the owner's
    served snapshot."""
    return rig.tick(tracked_url, owner_url, failures)[1]


def drive_until(rig, tracked_url, owner_url, failures, condition,
                bound=SETTLE_BOUND):
    """Driven pair ticks until `condition(owner_snapshot)` holds —
    returns the satisfying snapshot, or None when `bound` scans pass
    without it landing."""
    for _ in range(bound):
        owner = tick(rig, tracked_url, owner_url, failures)
        if condition(owner):
            return owner
    return None


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
        choices=["expect-reconsumed", "expect-cleared"],
        help="doctor the leg's own expectation — the pass must fail "
        "naming the evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = ack_edge_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            doctored = {
                "expect-reconsumed": "the doctored expectation wanted a "
                "re-consumed edge",
                "expect-cleared": "the doctored expectation wanted the "
                "held press to clear the latch",
            }[args.tamper]
            eprint(
                f"ack-edge-lifecycle: {doctored} — an inconclusive "
                "run offers the doctored case no evidence"
            )
            return 1
        eprint(f"ack-edge-lifecycle: inconclusive — {inconclusive}")
        print(f"ack-edge-lifecycle-digest inconclusive — {inconclusive}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"ack-edge-lifecycle: {line}")
        return 1
    for failure in failures:
        eprint(f"ack-edge-lifecycle: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"ack-edge-lifecycle: the {args.tamper} case passed "
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
        f"ack-edge-lifecycle-digest {digest} — tracking by tick "
        f"{evidence['converged']}, tripped at tick "
        f"{evidence['tripped_at']}, consumed once at tick "
        f"{evidence['consumed_at']}, re-latched at tick "
        f"{evidence['relatched_at']} under the held level, promoted "
        f"at tick {evidence['promoted_at']} with the latch standing, "
        f"the dropped release held through tick "
        f"{evidence['drop_held_at']}, the held press settled at tick "
        f"{evidence['held_press_at']}, released at tick "
        f"{evidence['released_at']}, recovered at tick "
        f"{evidence['recovered_at']}, restored at tick "
        f"{evidence['restored_at']}, {evidence['entries']} lifecycle "
        "records audited per pair"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
