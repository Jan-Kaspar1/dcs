#!/usr/bin/env python3
"""The release-orphan-window leg for the reference plant — the
consumer-boundary mirror of the qa rig's `release_orphan_window` leg
(#1179), pinned on the manifest-declared redundant pair and never
before on the customer-owned deployment: a `POST /demote` landing
inside the interlock-release propagation window **bounds** the
energized orphan window (WW-ENG-003, WW-LCM-001).

The consumer model wires the protection-layer contact into
availability exactly as the platform's own fixtures do —
`power-fail` → `power-ok` → each pump's `power-ok-in` feeding its
`avail_i` aggregate, with the protection interlock guarding the
command — and carries the writable `p10x-mode`/`p10x-hand` operator
points the receipted path drives. The release therefore propagates
over driven scans, never on the write: a legal operator demote landing
between the contact write and the scan that writes the released
`p10x-cmd` abandons the in-flight write and leaves the field
energized while no peer owns it. The run:

- converges the manifest-declared pair through the pair harness's
  driven-tick loop — the field owner `active`, the declared standby
  `tracking` — and joins the field's standing writer claim under the
  owner's reported `--owner-token`, the token the leg's plant-protocol
  attachment rides;
- hand-runs both pumps through the bounded receipted operator path:
  the four `p10x-mode`/`p10x-hand` writes admitted on the field
  owner, each settling `applied` in the adopted receipt log, with both
  `p10x-cmd` field outputs reading energized through the plant
  protocol and in the served image — the healthy precondition the
  trip interrupts;
- drives the journaled `power-fail` contact through the plant
  protocol, asserting both outputs still read energized when the
  write lands — the release lands on the deterministic scan sequence,
  never on an out-of-band step — and then demotes the field owner in
  the same breath, so the demotion lands inside the release window
  with no scan between the write and the role change;
- watches the energized orphan window across the declared bound of
  `RELEASE_SCANS` driven pair ticks: each row carries the field's own
  reads of both outputs, the served trip carriers that attribute a
  release to the declared interlock chain, and both peers' role
  reports. Neither peer may report the promote-blocking `diverged`
  verdict — the staged-versus-field comparison on an ownerless line
  the finding recorded — and the orphan transition must journal on the
  peer whose pull landed it;
- promotes the declared standby **inside** the bound: `POST /promote`
  is answered `promoting` (never the `not_converged` wedge that left
  the pair with no active able to write the release), and the promoted
  peer's first field-owning scans write the abandoned release, so the
  field never reads energized past the bound;
- asserts the pair reconverges on one active plus one tracking
  standby, then restores what the leg drove: the contact released on
  the field, both pumps returned to the operator state it found them
  in through the receipted path, the consequential alarm latches
  acknowledged and their ack inputs re-armed, and the pair back on
  its declared launch roles with the field owner owning the field.

The contract postdates the pinned artifact set — a release cut before
it reads this way until one carries it: where the ownerless window's
own rows report `diverged` and the standby's promote answers
`not_converged`, the run's evidence is the pre-#828 shape and the leg
reports `release-orphan-window-digest inconclusive` rather than
asserting, until the manifest repins a release carrying the contract.

Usage:

    release_orphan_window.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `release-orphan-window-digest <sha256>` line prints —
the check runs two passes and compares them
(`release-orphan-window-nondeterministic`; the pair stage forms the
nondeterministic and unchecked names on the leg's file stem, while the
contract's own failed diagnostic is the declared
`release-orphan-window-failed`). A contract violation reports
`release-orphan-window: …` lines on stderr and exits 1. The doctored
case `--tamper expect-flushed` flips the leg's own expectation to the
leg's other half — asserting the pending safe-state write landed
before the demotion completed — and `--tamper skip-promote` drops the
promote the bound rides, so each pass must fail naming the energized
window it actually observed, proving the bound's own assertion fires
on the honest episode rather than passing unexercised.
"""

import argparse
import json
import os
import re
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import foreign_claim_release
import pair
import power_trip
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its two
# digest-identical passes, and exercise its doctored cases. The
# doctored cases: the leg asserting the pending safe-state write
# landed before the demotion completed (the contract's other half)
# while the field stood energized, and the leg dropping the promote
# the bound rides — both must surface the named evidence on the honest
# episode.
LEG = {
    # The next free slot between demote-reconvergence (200) and
    # managed-lifecycle (210) — the stage runs the legs in this order
    # and no two may share one.
    "order": 205,
    "title": "the release-orphan-window leg",
    "passes": "release-orphan-window-leg",
    "failed": "release-orphan-window-failed",
    "tampers": [
        {
            "name": "expect-flushed",
            "passed": "an expect-flushed case passed the "
            "release-orphan-window leg",
            "missed": "the expect-flushed case did not report its "
            "named evidence",
            "evidence": [
                "the doctored expectation wanted the safe-state write "
                "to land before the demotion completed",
            ],
        },
        {
            "name": "skip-promote",
            "passed": "a skip-promote case passed the "
            "release-orphan-window leg",
            "missed": "the skip-promote case did not report its named "
            "evidence",
            "evidence": [
                "the doctored expectation dropped the promote the "
                "bound rides",
            ],
        },
    ],
}


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort
Inconclusive = foreign_claim_release.Inconclusive

# The declared bounds, in driven pair ticks — the rig's only clock.
# HAND_SCANS is the bound on the receipted hand run reaching the field:
# the admitted writes settle at the owner's next scan boundary and the
# group's image crosses one carrier hop per scan. WINDOW_ROUNDS is the
# answered window the leg observes before it promotes, and
# RELEASE_SCANS is **the declared orphan-window bound**: the energizing
# field may not survive that many driven ticks across the demote,
# because the promoted successor writes the abandoned release on its
# first field-owning scan and the interlock chain needs a handful of
# scans to carry the trip at all. It is deliberately far above both —
# the finding measured ~1s of operator-bounded window on the rig at a
# 100 ms cadence — so the bound admits a slow rig while still naming
# the shape the contract forbids: an energization that only an operator
# clears, with no controller able to take the field.
HAND_SCANS = 8
WINDOW_ROUNDS = 3
RELEASE_SCANS = 12
SETTLE_TICKS = 4
ACTOR = "ci-release-orphan-window"

# The signal names resolving the leg's point ids out of the emitted
# model — the same names the signal index serves, so the leg
# exercises the declared seam, never a hard-coded id.
SIGNALS = {
    "power_fail": "power-fail",
    "power_ok": "power-ok",
    "avail_1": "p101-avail",
    "avail_2": "p102-avail",
    "cmd_1": "p101-cmd",
    "cmd_2": "p102-cmd",
    "mode_1": "p101-mode",
    "hand_1": "p101-hand",
    "mode_2": "p102-mode",
    "hand_2": "p102-hand",
    "power_unack": "power-fail-unacknowledged",
    "power_ack": "power-fail-ack",
    "none_unack": "none-available-unacknowledged",
    "none_ack": "none-available-ack",
}
# The operator points the leg hands the pumps over with and returns
# them through.
OPERATORS = ("mode_1", "hand_1", "mode_2", "hand_2")
# The field outputs the bound is judged on.
COMMANDS = ("cmd_1", "cmd_2")

# The stable reason a predating release reports, and the stable
# evidence prefixes the doctored cases leave behind.
PRE_CONTRACT_REASON = (
    "the pinned release predates the bounded interlock-release "
    "orphan-window contract"
)
TAMPER_EXPECT_FLUSHED = (
    "the doctored expectation wanted the safe-state write to land "
    "before the demotion completed"
)
TAMPER_SKIP_PROMOTE = (
    "the doctored expectation dropped the promote the bound rides"
)


def signal_points(model):
    """The `{key: point id}` map the emitted model's signal index
    declares for the leg's driven and reported points — None when the
    model declares no such protection seam. A signal's `source` is the
    point it names; the lowest-signal-id-wins rule the served index
    applies. The four operator points the receipted hand run drives
    must be declared writable — the leg's own bounded path."""
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
        point["id"]
        for point in model.get("io_points", [])
        if point.get("writable")
    }
    if any(points[key] not in writable for key in OPERATORS):
        return None
    return points


def sync_word(report):
    """The served `StandbySync`'s variant name — `unsynchronized` and
    `degraded` are bare strings, the rest single-key objects."""
    sync = (report or {}).get("sync")
    if isinstance(sync, str):
        return sync
    if isinstance(sync, dict) and sync:
        return next(iter(sync))
    return None


def served_role(report):
    """The three facts this leg asserts on, projected off a served
    RoleReport: the role, the sync verdict, and the served tick. The
    whole report stays in the run's stderr evidence — a degraded sync
    carries its transport error text and a held claim carries the
    claim's ephemeral declared monitor, neither of which two identical
    passes can share, while these three are the pair's own posture."""
    return {
        "role": (report or {}).get("role"),
        "sync": sync_word(report),
        "tick": (report or {}).get("tick"),
    }


def energized(sample):
    """Whether a field read's stored value reads energized."""
    return (sample or {}).get("bool") is True


def journal_orphans(entries):
    """The `field_orphaned` records a served journal carries — the
    durable half of the ownerless-line transition."""
    found = []
    for entry in entries or []:
        event = (entry.get("event") or {}).get("field_orphaned")
        if isinstance(event, dict):
            found.append(event)
    return found


def journal_walk(entries):
    """The journaled `role_changed` transitions as (from, to, origin)
    tuples."""
    walk = []
    for entry in entries or []:
        change = (entry.get("event") or {}).get("role_changed")
        if isinstance(change, dict):
            walk.append((change.get("from"), change.get("to"),
                         change.get("origin")))
    return walk


def submit(url, command, failures):
    """POST one receipted operator write to the field owner and assert
    the `accepted` submission — the bounded path the hand run and the
    restore both drive."""
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


def window_scan(rig, failures):
    """One driven pair tick of the ownerless window: the declared
    standby scanned first so its pull applies the demoted owner's latest
    checkpoint, then the demoted owner itself.

    The shared harness's `tick` also asserts the two images are
    identical, which is the right discipline for a converged pair and
    the wrong one here: the demoted member's gate is closed, so its
    scans are quiesced — they carry an adopted receipt instead of
    settling it (#689) — and its image legitimately trails the
    tracking peer's by the abandoned write while no peer owns the
    field. The window watches the field and both serving monitors
    scan by scan; the identical-image assertion returns with the
    documented switch's bumplessness check in the restore phase, once
    the pair is a working pair again. Returns the owner's served
    snapshot."""
    pair.scan(rig.standby_url, failures)
    return pair.scan(rig.duty_url, failures)


def window_row(rig, plant_io, points, failures):
    """One row of the orphan window: the plant protocol's own field
    reads of both outputs and both peers' served role reports — the
    served evidence tuple every clause of the bound reads."""
    row = {"tick": None, "commands": {}, "power_ok": None, "avail": [],
           "duty": None, "standby": None}
    for key in COMMANDS:
        row["commands"][key] = energized(
            power_trip.field_read(plant_io, points[key], failures)
        )
    row["duty"] = pair.get(f"{rig.duty_url}/role", "GET /role", failures)
    row["standby"] = pair.get(
        f"{rig.standby_url}/role", "GET /role", failures
    )
    row["power_ok"] = row["duty"].get("power_ok")
    row["avail"] = [row["duty"].get(f"avail_{index}") for index in (1, 2)]
    return row


def release_orphan_window_pass(args, tamper):
    """The bounded orphan-window run: converge, hand-run, trip, demote
    inside the release window, watch the energized orphan window across
    the declared bound, promote inside it, assert the flush and the
    reconvergence, and restore the field state and the launch roles.
    Returns `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "release-orphan-window leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    with open(args.model) as handle:
        model = json.load(handle)
    points = signal_points(model)
    if points is None:
        raise Abort(
            "the emitted model declares no power-fail protection seam "
            "with the writable pump operator points — the leg has "
            "nothing to exercise"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        plant_io = rig.plant_io
        # The field owner's writer claim: a release that claims at
        # startup reports the owner token ahead of its listener; where
        # no token is reported the field stands unclaimed and the leg's
        # field writes land unfenced.
        owner_token = None
        for line in rig.duty_preamble:
            claimed = re.search(r"owner token (\d+)", line)
            if claimed:
                owner_token = int(claimed.group(1))
        evidence["owner_token"] = "reported" if owner_token else "none"

        # Phase 1 — convergence, through the shared harness's driven
        # ticks: the tracking peer scanned first so each pull applies
        # the owner's latest checkpoint and the peers rest identical.
        converged = rig.converge(failures)
        evidence["converged"] = converged["ticks"][-1]
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": served_role(converged["duty_role"]),
                "standby_role": served_role(converged["standby_role"]),
            }
        )
        duty_floor = len(pair.get(
            f"{duty_url}/journal", "GET /journal", failures) or [])
        standby_floor = len(pair.get(
            f"{standby_url}/journal", "GET /journal", failures) or [])

        # Phase 2 — the hand run: both pumps taken onto the operator's
        # hand through the bounded receipted path, so both field
        # outputs stand energized on the plant and in the served image
        # whatever the level does.
        found = {
            key: power_trip.flag(converged["owner"], points[key])
            for key in OPERATORS
        }
        standing = sorted(key for key, value in found.items() if value)
        if standing:
            raise Inconclusive(
                "the declared pumps already stand in hand",
                f"the operator points {standing} are driven at launch — "
                "the rig cannot hand them over through its own "
                "receipted path",
            )
        submitted = []
        for key in OPERATORS:
            command = power_trip.write_value(points[key], True)
            submit(duty_url, command, failures)
            submitted.append({"point": points[key], "key": key,
                              "command": command})
        owner = converged["owner"]
        energized_at = None
        for _ in range(HAND_SCANS):
            owner = power_trip.tick(rig, failures)
            if all(power_trip.flag(owner, points[key])
                   for key in COMMANDS):
                energized_at = owner["tick"]
                break
        if energized_at is None:
            failures.append(
                "the receipted hand run never energized both field "
                f"outputs — {COMMANDS} read "
                f"{power_trip.flag(owner, points['cmd_1'])} / "
                f"{power_trip.flag(owner, points['cmd_2'])} after "
                f"{HAND_SCANS} driven scans"
            )
            raise Abort
        receipts = pair.get(f"{duty_url}/receipts", "GET /receipts",
                            failures)
        unsettled = [
            item["key"] for item in submitted
            if not power_trip.settled(receipts, item["command"])
        ]
        if unsettled:
            failures.append(
                "the receipted hand run never settled applied for: "
                + ", ".join(unsettled)
            )
            raise Abort
        for key in COMMANDS:
            if not energized(power_trip.field_read(plant_io, points[key],
                                                   failures)):
                failures.append(
                    f"the field's {key} does not read energized — the "
                    "hand run's delivered command never reached the "
                    "field"
                )
                raise Abort
        evidence["energized_at"] = energized_at
        digest_entries.append(
            {
                "phase": "hand-run",
                "tick": energized_at,
                "commands": {
                    key: power_trip.flag(owner, points[key])
                    for key in COMMANDS
                },
            }
        )

        # Phase 3 — the trip and the raced demote. The pair is driven,
        # so no scan runs between the contact write and the role
        # change: the demotion lands inside the release-propagation
        # window by construction, and the field still reads energized
        # because the abandoned write is exactly the release that never
        # reached it.
        verdict = power_trip.field_write(
            plant_io, owner_token, points["power_fail"], True
        )
        if verdict.get("result") != "done":
            failures.append(
                f"the field write on power-fail answered {verdict}"
            )
            raise Abort
        before = {
            key: energized(
                power_trip.field_read(plant_io, points[key], failures))
            for key in COMMANDS
        }
        if not all(before.values()):
            failures.append(
                f"the field's commands stepped off outside the driven "
                f"scan sequence — {before} — the interlock's release "
                "moved without a scan"
            )
            raise Abort
        trip_tick = owner["tick"]
        demote = rig.demote(duty_url, failures)
        eprint(f"release-orphan-window: the field stayed energized "
               f"{before} through the demote answered "
               f"{demote.get('role')} at tick {trip_tick}")
        evidence["tripped_at"] = trip_tick
        digest_entries.append(
            {
                "phase": "trip",
                "tick": trip_tick,
                "commands": before,
                "demote": served_role(demote),
            }
        )

        # Phase 4 — the bounded orphan window, watched across the
        # declared bound. The window's own rows decide which half of
        # the contract this pass observed: the energization standing
        # across the demote (the orphan window, promoted out of) or the
        # pending safe-state write reaching the field before the
        # successor takes it (the contract's first half). Either way
        # the promote runs, so the pair reconverges and the abandoned
        # release is written by a controller that owns the field; the
        # pre-contract signature — the promote-blocking `diverged` with
        # the successor's promotion refused `not_converged` — is
        # classified before any assertion reads it.
        rows = []
        promoted = None
        refusal = None
        energized_rows = 0
        flushed = False
        for _ in range(RELEASE_SCANS):
            rows.append(window_row(rig, plant_io, points, failures))
            owner = window_scan(rig, failures)
            if not any(rows[-1]["commands"].values()):
                flushed = True
                break
            energized_rows += 1
            if energized_rows < WINDOW_ROUNDS:
                continue
            if tamper == "skip-promote":
                refusal = {"status": None, "body": TAMPER_SKIP_PROMOTE}
                break
            status, body = pair.request(f"{standby_url}/promote", {})
            if status != 200:
                refusal = {"status": status, "body": body}
            else:
                promoted = body
            break
        if promoted is None and refusal is None:
            # The window ended without the energization clearing and
            # without a promote: the field would stand energized with no
            # controller able to take it — the shape the contract
            # forbids. The doctored case that drops the promote lands
            # here by name.
            if tamper == "skip-promote":
                refusal = {"status": None, "body": TAMPER_SKIP_PROMOTE}
            else:
                refusal = {
                    "status": None,
                    "body": "the declared bound elapsed with the field "
                    "still energized and no promotion issued",
                }
        diverged = [
            row for row in rows
            if sync_word(row["duty"]) == "diverged"
            or sync_word(row["standby"]) == "diverged"
        ]
        if diverged and refusal is not None \
                and "not_converged" in json.dumps(refusal) \
                and tamper is None:
            # The pre-#828 shape: the quiesced peers compared their
            # staged released image against the still-energized field
            # and the successor's promotion was gated on it forever.
            raise Inconclusive(
                PRE_CONTRACT_REASON,
                f"the ownerless window left the peers reporting "
                f"{[sync_word(row['standby']) for row in rows]} and the "
                f"promote answered {refusal} — no controller could take "
                "the field to write the release",
            )
        if refusal is not None:
            failures.append(
                f"POST /promote on the declared standby answered "
                f"{refusal} — inside the declared {RELEASE_SCANS}-scan "
                "bound the divergence gate must never leave the field "
                "energized with no controller able to take it"
            )
            raise Abort
        if diverged:
            failures.append(
                "a peer reported the promote-blocking `diverged` verdict "
                "inside the ownerless window — a staged-versus-field "
                "comparison on a line no peer owns: "
                f"{[served_role(row['standby']) for row in rows]}"
            )
            raise Abort
        if tamper == "expect-flushed" and not flushed:
            failures.append(
                f"{TAMPER_EXPECT_FLUSHED} — the field read {before} when "
                "the demotion completed, and the release the successor "
                "wrote landed only on its first field-owning scan, "
                f"{len(rows)} driven scans later"
            )
            raise Abort
        evidence["shape"] = "flushed" if flushed else "orphan"
        evidence["window"] = [
            {"commands": row["commands"], "duty": served_role(row["duty"]),
             "standby": served_role(row["standby"])}
            for row in rows
        ]
        evidence["promoted_at"] = served_role(promoted)

        # Phase 5 — the flush inside the bound: the promoted peer's
        # first field-owning scans write the abandoned release, so the
        # field stops reading energized inside the declared scans.
        released_at = None
        flushed = []
        for _ in range(RELEASE_SCANS):
            owner = window_scan(rig, failures)
            commands = {
                key: energized(
                    power_trip.field_read(plant_io, points[key],
                                          failures))
                for key in COMMANDS
            }
            flushed.append({"tick": owner["tick"], "commands": commands})
            if not any(commands.values()):
                released_at = owner["tick"]
                break
        if released_at is None:
            failures.append(
                "the field outputs stood energized past the declared "
                f"{RELEASE_SCANS}-scan orphan-window bound — the "
                "promoted successor's field-owning scans never wrote "
                f"the abandoned release: {flushed[-1]}"
            )
            raise Abort
        if not all(
                power_trip.flag(owner, points[key]) is False
                for key in COMMANDS):
            failures.append(
                f"the promoted successor released the commands on the "
                f"field but its own image still stages them — {owner}"
            )
        evidence["released_at"] = released_at
        evidence["release_scans"] = len(flushed)
        digest_entries.append(
            {
                "phase": "release",
                "tick": released_at,
                "scans": len(flushed),
                "bound": RELEASE_SCANS,
                "promote": served_role(promoted),
                "rows": evidence["window"],
                "flushed": flushed,
            }
        )

        # Phase 6 — the reconvergence and the durable trail: one
        # active plus one tracking standby, the orphan transition
        # journaled by name on the peer whose pull landed it, and both
        # declared journal files carrying the episode.
        for _ in range(SETTLE_TICKS):
            window_scan(rig, failures)
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        standby_role = pair.get(
            f"{standby_url}/role", "GET /role", failures)
        if standby_role.get("role") != "active":
            failures.append(
                f"the promoted peer reports "
                f"{standby_role.get('role')!r}, expected active"
            )
            raise Abort
        if duty_role.get("role") != "standby" \
                or "tracking" not in (duty_role.get("sync") or {}):
            failures.append(
                f"the demoted peer never reconverged on the promoted "
                f"owner — GET /role answers {duty_role}"
            )
            raise Abort
        journals = {
            duty_decl["name"]: pair.get(
                f"{duty_url}/journal", "GET /journal", failures),
            standby_decl["name"]: pair.get(
                f"{standby_url}/journal", "GET /journal", failures),
        }
        orphans = {
            name: journal_orphans(entries)
            for name, entries in journals.items()
        }
        if not any(orphans.values()):
            failures.append(
                "no peer journaled a field_orphaned record for the "
                "ownerless line the demotion left"
            )
            raise Abort
        walk = {
            name: journal_walk(entries)
            for name, entries in journals.items()
        }
        if not any(row[1] == "demoting" for row in
                   walk[duty_decl["name"]]):
            failures.append(
                f"{duty_decl['name']}'s journal carries no walk into "
                f"demoting — the leg's own demote left no durable "
                f"record: {walk[duty_decl['name']]}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "reconverge",
                "duty_role": served_role(duty_role),
                "standby_role": served_role(standby_role),
                "orphans": {name: len(found_)
                            for name, found_ in orphans.items()},
                "journals": {name: len(entries)
                             for name, entries in journals.items()},
                "floors": {"duty": duty_floor, "standby": standby_floor},
            }
        )

        # Phase 7 — the restore: the pair back on its declared launch
        # roles through the documented order, the driven contact
        # released on the field, both pumps returned to the operator
        # state the leg found, and the consequential alarm latches
        # acknowledged and their ack inputs re-armed.
        rig.switch(standby_url, duty_url, failures,
                   promote_note=" back to the declared duty member")
        verdict = power_trip.field_write(
            plant_io, owner_token, points["power_fail"], False
        )
        if verdict.get("result") != "done":
            failures.append(
                f"the restoring field write on power-fail answered "
                f"{verdict} — the leg cannot hand the contact back"
            )
            raise Abort
        if energized(power_trip.field_read(plant_io, points["power_fail"],
                                          failures)):
            failures.append(
                "the driven power-fail contact did not read released "
                "again — the deployment is left tripped under a leg "
                "that drove it"
            )
            raise Abort
        restored = {}
        for key in OPERATORS:
            command = power_trip.write_value(points[key], False)
            submit(duty_url, command, failures)
            restored[key] = command
        final = power_trip.tick(rig, failures)
        standing = sorted(
            key for key in OPERATORS
            if power_trip.flag(final, points[key])
        )
        if standing:
            failures.append(
                "the pumps the leg hand-ran were left in a driven "
                f"operator state: {standing}"
            )
            raise Abort
        if power_trip.flag(final, points["power_fail"]):
            failures.append(
                "the served power-fail contact still reads asserted "
                "after the restoring write"
            )
            raise Abort
        latched = acknowledge_latches(duty_url, plant_io, points,
                                      failures)
        final = power_trip.tick(rig, failures)
        evidence["restored_at"] = final["tick"]
        digest_entries.append(
            {
                "phase": "restore",
                "tick": final["tick"],
                "operators": sorted(restored),
                "latched": sorted(latched),
                "duty_role": served_role(
                    pair.get(f"{duty_url}/role", "GET /role", failures)),
                "standby_role": served_role(
                    pair.get(f"{standby_url}/role", "GET /role",
                             failures)),
            }
        )
    except Inconclusive:
        raise
    except Abort as abort:
        failures.extend(str(argument) for argument in abort.args)
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        if rig is not None:
            rig.close()
    return digest_entries, evidence, failures


def acknowledge_latches(duty_url, plant_io, points, failures):
    """Walk the consequential alarm latches the trip raised toward the
    state the leg found them in: acknowledge a latched unacknowledged
    flag while its ack input reads released, then re-arm the ack input
    once the flag reads clear. Cleanup, never a clause — a refused
    write is reported and the run continues."""
    touched = []
    for flag, ack in (("power_unack", "power_ack"),
                      ("none_unack", "none_ack")):
        snapshot = pair.get(f"{duty_url}/snapshot", "GET /snapshot",
                            failures)
        if power_trip.flag(snapshot, points[flag]) \
                and not power_trip.flag(snapshot, points[ack]):
            command = power_trip.write_value(points[ack], True)
            submit(duty_url, command, failures)
            touched.append(flag)
        snapshot = pair.get(f"{duty_url}/snapshot", "GET /snapshot",
                            failures)
        if not power_trip.flag(snapshot, points[flag]) \
                and power_trip.flag(snapshot, points[ack]):
            command = power_trip.write_value(points[ack], False)
            submit(duty_url, command, failures)
            touched.append(ack)
    return touched


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
        choices=["expect-flushed", "skip-promote"],
        help="doctor the leg's own expectations — the pass must fail "
        "naming the energized window it observed",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = (
            release_orphan_window_pass(args, args.tamper)
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "release-orphan-window: the doctored case is offered no "
                "evidence by an inconclusive run"
            )
            return 1
        reason = inconclusive.args[0]
        detail = inconclusive.args[-1]
        eprint(f"release-orphan-window: inconclusive — {detail}")
        print(f"release-orphan-window-digest inconclusive — {reason}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"release-orphan-window: {line}")
        return 1
    for failure in failures:
        eprint(f"release-orphan-window: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"release-orphan-window: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored "
                "expectation"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"release-orphan-window-digest {digest} — the manifest-declared "
        f"pair converged by tick {evidence['converged']}, both pumps "
        f"stood hand-run and energized by tick {evidence['energized_at']}, "
        f"the power-fail contact tripped at tick "
        f"{evidence['tripped_at']} with the field still energized, and "
        f"the demote inside that window left it energized across "
        f"{len(evidence['window'])} driven scans of the declared "
        f"{RELEASE_SCANS}-scan bound — no peer reporting the "
        "promote-blocking diverged — the standby's promotion answered "
        f"promoting and its first field-owning scans wrote the release "
        f"at tick {evidence['released_at']}, {evidence['release_scans']} "
        "scans inside the bound; the pair reconverged to one active "
        "plus one tracking standby and the contact released with both "
        f"pumps back on their launch operator state at tick "
        f"{evidence['restored_at']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())