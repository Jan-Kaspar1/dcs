#!/usr/bin/env python3
"""The unacknowledged-alarm latch carryover leg for the reference plant
— the consumer-side proof that a standing managed alarm's
`unacknowledged` latch survives a redundant promotion (WW-ENG-003,
WW-LCM-001, WW-ALM-001).

WW-LCM-001's station-facing acceptance names unacknowledged alarm
latches among the operator-visible state that must carry across a
takeover, and the emitted consumer model declares the vocabulary to
prove it: the managed-bool-latching-alarm set with journaled lifecycle
points and a field-driven trigger (`power-fail`, point 14, device 2 —
a journaled field In point a plant-protocol write can drive). The pair
stage's persisted-journal checks cover role transitions, and the
in-flight command/force legs cover receipts and forcing, but the alarm
lifecycle's checkpoint carriage is unproven on a customer-shaped
deployment. This leg closes it, using the ci scripts' existing
plant-protocol client (reference-plant/ci/simulate.py's line-delimited
`Plant`):

- with the pair settled and tracking, write the field input that
  raises the declared `power-fail` managed alarm on the shared plant
  and assert through the active's monitor that `alarm` and
  `unacknowledged` assert with the journaled activation;
- demote/promote and assert on the promoted peer that both flags still
  stand — the unacknowledged latch carried through the checkpoint
  rather than cleared or re-armed — with no re-journaled activation
  on the new peer;
- submit the managed `ack` through the receipted path on the new
  active and assert the settled receipt, `unacknowledged` clearing
  while `alarm` persists for the standing condition, and the
  attributed acknowledgment journaled on the promoted peer;
- clear the field input, assert the alarm's declared return behavior,
  and restore the pair's roles.

Usage:

    latch_carryover.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `latch-carryover-digest <sha256>` line prints — the
check runs two passes and compares them
(`latch-carryover-nondeterministic`). A contract violation reports
`latch-carryover: …` lines on stderr and exits 1 — the check's
`latch-carryover-failed`. `--tamper lost-latch` drops the promoted
peer's `unacknowledged` assertion from the record the audit reads and
`--tamper rejournaled-activation` plants a second activation on the
promoted peer — a promoted peer that lost the latch or re-journaled
the activation must fail the audit rather than pass silently.
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
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored cases: a promoted peer that lost the unacknowledged
# latch, or one that re-journaled the activation, must surface the
# named diagnostic — never a silently unexercised carryover proof.
LEG = {
    "order": 115,
    "title": "the alarm-latch carryover leg",
    "passes": "latch-carryover",
    "tampers": [
        {
            "name": "lost-latch",
            "passed": "a lost-latch journal passed the latch-carryover leg",
            "missed": "the lost-latch case did not report its named diagnostic",
            "evidence": ["unacknowledged latch did not carry"],
        },
        {
            "name": "rejournaled-activation",
            "passed": "a rejournaled-activation journal passed the latch-carryover leg",
            "missed": "the rejournaled-activation case did not report its named diagnostic",
            "evidence": ["re-journaled the activation"],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The driven ticks each phase runs — the bound each phase's declared
# effect gets to land across the wiring's one-scan carrier crossings.
# The actor the leg's receipted submissions declare — the attribution
# every settled receipt must carry.
SETTLE_BOUND = 16
ACTOR = "ci-latch-carryover"

# The signal names resolving the leg's point ids out of the emitted
# model — the same names the signal index serves, so the leg exercises
# the declared seam, never a hard-coded id.
SIGNALS = {
    "power_fail": "power-fail",
    "power_ack": "power-fail-ack",
    "power_alarm": "power-fail-alarm",
    "power_unack": "power-fail-unacknowledged",
}


def signal_points(model):
    """The `{key: point id}` map the emitted model's signal index
    declares for the leg's latch — None when the model declares no
    such alarm. A signal's `source` is the point it names; the
    lowest-signal-id-wins rule the served index applies. The field
    contact must be a journaled field `In`, the ack a writable
    internal `In` — the seams the leg drives and commands."""
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
    contact = declared.get(points["power_fail"], {})
    if (
        contact.get("direction") != "in"
        or contact.get("value_type") != "bool"
        or contact.get("channel") is None
        or not contact.get("journaled")
    ):
        return None
    ack = declared.get(points["power_ack"], {})
    if (
        ack.get("direction") != "in"
        or ack.get("value_type") != "bool"
        or not ack.get("writable")
    ):
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
    pass without it landing."""
    for _ in range(SETTLE_BOUND):
        owner = tick(rig, failures)
        if condition(owner):
            return owner
    return None


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
    applied under the leg's actor."""
    return any(
        entry.get("command") == command
        and simulate.receipt_outcome(entry) == "applied"
        and entry.get("actor") == ACTOR
        for entry in receipts
    )


def journal_events(entries):
    """The leg's audit stream out of a journal entry list —
    `("settled", point, value, outcome, actor)` for each command
    receipt, `("changed", point, to)` for each journaled value
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


def activations(entries, alarm_point):
    """The journaled activation ticks for the alarm point — the
    `point_changed` transitions to true, in `seq` order. A carried
    latch adds none; a re-armed latch appends one."""
    return [
        entry.get("tick")
        for entry in entries
        if entry.get("event", {}).get("point_changed", {}).get("point")
        == alarm_point
        and entry.get("event", {}).get("point_changed", {}).get("to")
        == {"bool": True}
    ]


def standing_owner(plant_io):
    """The owner token the field's standing write-ownership claim
    asserts — the read-only `probe_writer` verdict names the holder's
    token, answers `done` while this attachment holds the claim itself,
    and `unclaimed` while none stands. The probe mutates nothing, so
    reading the field's ownership cannot move it: the leg re-learns
    the holder after a promotion moved the claim, rather than driving a
    restore write through the launch owner's now-stale token and
    meeting the field's own fencing refusal."""
    verdict = plant_io.request({"op": "probe_writer"})
    return (verdict.get("error") or {}).get("owner")


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


def carryover_pass(args, tamper):
    """The carryover run: converge, trip, promote, ack, return,
    restore, audit. Returns `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the carryover leg "
            "has nothing to exercise"
        )
    with open(args.model) as handle:
        model = json.load(handle)
    points = signal_points(model)
    if points is None:
        raise Abort(
            "the emitted model declares no power-fail latch seam — "
            "the leg has nothing to exercise"
        )
    true, false = {"bool": True}, {"bool": False}
    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared, tamper)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        plant_io = rig.plant_io
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
        if value(owner, points["power_alarm"]) != false or value(
            owner, points["power_unack"]
        ) != false:
            failures.append(
                "the converged pair is not quiet — the power-fail "
                f"alarm reads {value(owner, points['power_alarm'])}, "
                "unacknowledged "
                f"{value(owner, points['power_unack'])} before the leg "
                "drives"
            )
            raise Abort

        # Phase 2 — the field-driven trip: the `power-fail` contact
        # written through the plant protocol raises the declared
        # managed alarm — `alarm` and `unacknowledged` asserting on
        # the active's monitor with the journaled activation.
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
            == true
            and value(snapshot, points["power_unack"]) == true,
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                "the field-held power-fail never annunciated — alarm "
                f"reads {value(owner, points['power_alarm'])}, "
                f"unacknowledged {value(owner, points['power_unack'])}"
            )
            raise Abort
        evidence["tripped_at"] = owner["tick"]
        duty_activations = activations(
            journal_entries(rig.duty_files), points["power_alarm"]
        )
        if not duty_activations:
            failures.append(
                "the field owner's journal carries no activation for "
                "the driven power-fail alarm — the trip is unjournaled"
            )
            raise Abort
        standby_activations = activations(
            journal_entries(rig.standby_files), points["power_alarm"]
        )
        digest_entries.append(
            {
                "phase": "trip",
                "tripped_at": owner["tick"],
                "activations": len(duty_activations),
            }
        )

        # Phase 3 — the promotion: demote/promote with the latch
        # standing. The promoted peer must report both flags still
        # standing — the unacknowledged latch carried through the
        # checkpoint rather than cleared or re-armed — with no
        # re-journaled activation on the new peer.
        switch = rig.switch(
            duty_url, standby_url, failures, audit_receipts=True
        )
        owner = switch["owner"]
        alarm = value(owner, points["power_alarm"])
        unack = value(owner, points["power_unack"])
        if tamper == "lost-latch":
            # Doctor the record the audit reads: the promoted peer
            # lost the unacknowledged latch — the audit must name it.
            unack = false
        if alarm != true or unack != true:
            failures.append(
                "the unacknowledged latch did not carry across the "
                f"promotion — the promoted peer reads alarm "
                f"{value(owner, points['power_alarm'])}, "
                "unacknowledged "
                f"{value(owner, points['power_unack'])}, expected both "
                "standing"
            )
            raise Abort
        promoted_activations = activations(
            journal_entries(rig.standby_files), points["power_alarm"]
        )
        if tamper == "rejournaled-activation":
            promoted_activations = promoted_activations + [owner["tick"]]
        if len(promoted_activations) != len(standby_activations):
            failures.append(
                f"the promoted peer re-journaled the activation — the "
                f"new peer's journal carries "
                f"{len(promoted_activations)} power-fail activations "
                f"against {len(standby_activations)} before the switch"
            )
            raise Abort
        evidence["promoted_at"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "promote",
                "ticks": switch["ticks"],
                "activations": len(promoted_activations),
            }
        )

        # Phase 4 — the receipted ack on the new active: the managed
        # `ack` write settles `applied` under the leg's actor, the
        # `unacknowledged` latch clears while the standing `alarm`
        # persists, and the attributed acknowledgment journals on the
        # promoted peer.
        ack_write = write_value(points["power_ack"], True)
        status, receipt = pair.request(
            f"{standby_url}/command",
            {"command": ack_write, "actor": ACTOR},
        )
        if status != 200 or simulate.receipt_outcome(receipt) != "accepted":
            failures.append(
                f"the ack write on the promoted peer answered {status} "
                f"{receipt}, expected an accepted receipt"
            )
            raise Abort
        handover = None
        for _ in range(SETTLE_BOUND):
            _tracked, handover = rig.tick(
                duty_url, standby_url, failures
            )
            if value(handover, points["power_unack"]) == false:
                break
        if value(handover, points["power_unack"]) != false:
            failures.append(
                "the receipted ack never cleared the latch on the "
                "promoted peer — unacknowledged reads "
                f"{value(handover, points['power_unack'])}"
            )
            raise Abort
        if value(handover, points["power_alarm"]) != true:
            failures.append(
                "the ack cleared the standing alarm — alarm reads "
                f"{value(handover, points['power_alarm'])}, expected "
                "the standing condition to persist"
            )
            raise Abort
        receipts = pair.get(
            f"{standby_url}/receipts", "GET /receipts", failures
        )
        if not settled(receipts, ack_write):
            failures.append(
                "the ack write never settled applied under the leg's "
                "actor into the promoted peer's receipt log"
            )
            raise Abort
        if (
            "settled",
            points["power_ack"],
            true,
            "applied",
            ACTOR,
        ) not in journal_events(journal_entries(rig.standby_files)):
            failures.append(
                "the promoted peer's journal carries no attributed "
                "acknowledgment for the ack write"
            )
            raise Abort
        evidence["acknowledged_at"] = handover["tick"]
        submit(standby_url, write_value(points["power_ack"], False), failures)
        owner = rig.tick(duty_url, standby_url, failures)[1]

        # Phase 5 — the return and the restore: the cleared contact
        # returns the standing alarm, and the acknowledged state rides
        # the switch back to the manifest's declared roles. The write
        # lands through the promoted peer's own claim — the promotion
        # moved the field's write-ownership, so the launch owner's
        # token is stale here.
        owner_token = standing_owner(plant_io) or owner_token
        verdict = field_write(
            plant_io, owner_token, points["power_fail"], False
        )
        if verdict.get("result") != "done":
            failures.append(
                f"the restore write on power-fail answered {verdict}"
            )
            raise Abort
        returned = None
        for _ in range(SETTLE_BOUND):
            _tracked, returned = rig.tick(
                duty_url, standby_url, failures
            )
            if value(returned, points["power_alarm"]) == false and value(
                returned, points["power_unack"]
            ) == false:
                break
        if value(returned, points["power_alarm"]) != false or value(
            returned, points["power_unack"]
        ) != false:
            failures.append(
                "the cleared contact left the alarm standing — alarm "
                f"reads {value(returned, points['power_alarm'])}, "
                "unacknowledged "
                f"{value(returned, points['power_unack'])}"
            )
            raise Abort
        evidence["returned_at"] = returned["tick"]
        if owner_token is not None:
            verdict = plant_io.request({"op": "release_writer"})
            if verdict.get("result") != "done":
                failures.append(f"release_writer answered {verdict}")
                raise Abort
        back = rig.switch(standby_url, duty_url, failures)
        owner = back["owner"]
        for key in ("power_fail", "power_ack"):
            if value(owner, points[key]) != false:
                failures.append(
                    f"the leg left {key} standing — the point reads "
                    f"{value(owner, points[key])} at restore"
                )
        if failures:
            raise Abort
        evidence["restored_at"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "restore",
                "ticks": back["ticks"],
                "restored_at": owner["tick"],
            }
        )
        evidence["entries"] = len(journal_entries(rig.duty_files))
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
        choices=["lost-latch", "rejournaled-activation"],
        help="doctor the record the audit reads — the pass must fail "
        "naming the missing latch or the re-journaled activation",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = carryover_pass(args, args.tamper)
    except Abort as abort:
        for line in abort.args:
            eprint(f"latch-carryover: {line}")
        return 1
    for failure in failures:
        eprint(f"latch-carryover: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"latch-carryover: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored record"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"latch-carryover-digest {digest} — tracking by tick "
        f"{evidence['converged']}, tripped at tick "
        f"{evidence['tripped_at']}, promoted at tick "
        f"{evidence['promoted_at']}, acknowledged at tick "
        f"{evidence['acknowledged_at']}, returned at tick "
        f"{evidence['returned_at']}, restored at tick "
        f"{evidence['restored_at']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
