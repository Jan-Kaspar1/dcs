#!/usr/bin/env python3
"""The force-release leg for the reference plant — the consumer-side
proof that a receipted `unforce_point` on the deployed redundant pair
releases a standing operator force at a scan boundary: the badge
clears, the point's live value resumes, and both transitions journal
through the settled receipts (WW-ENG-003, WW-OPS-003 — decision 21's
release half).

The pair leg (`ci/pair.py`) proves the declared pair runs and
switches; the carryover leg (`ci/force_carryover.py`) proves a
standing force rides the promotion's adopted state. This leg proves
the release half the carry leaves unproven: an `unforce_point` that
cleared the badge while the substituted value kept driving control, or
a release that never settled, is exactly the substituted-data
dishonesty the confidence requirement names. The rig reads the
standby wiring and persistence fields out of `deploy/manifest.json`
and spawns the released tooling exactly as the pair legs do. The run:

- converges the declared standby to `tracking` through the pair leg's
  driven-tick loop, then records the force target's unforced sample —
  the control proving the unforced value is what would otherwise have
  served, which must differ from the forced value;
- submits `force_point` through `POST /command` on the active's
  monitor — the emitted model marks only *internal* `In` points
  writable, so the leg's `p101-hand` is the honest target, inert
  downstream while the pump stands in auto — asserting the `accepted`
  submission, the `applied` settlement identical in both peers'
  adopted log at the apply boundary's tick, the snapshot's `forces`
  entry and the `Uncertain(Substituted)` sample on both peers, the
  substituted stamp standing across further driven scans, and the
  active's served journal carrying the attributed settlement;
- submits `unforce_point` through the same receipted path, asserting
  the release at its apply tick: the `forces` set empty, the point
  resuming its unforced serve — for the internal target the
  held-value rule leaves the force's last stamp re-stamped `Good`,
  the observable state a same-value `write_value` produces — on both
  peers, and the `applied` settlement landing at that scan boundary
  in the identical adopted log;
- proves the live path truly resumed with a receipted `write_value`
  returning the pre-force held value — the point serving the written
  value at `Good`, its settlement adopted identically;
- issues the documented switch — `POST /demote` on the field owner,
  `POST /promote` on the converged standby — asserting the promoted
  peer serves the released state: no resurrected force, the live
  image carried like any run state; then restores the pair's launch
  roles — `demote` on the new owner, `promote` on the reconverged
  peer — leaving the manifest-declared duty controller `active` and
  its standby `tracking`;
- audits the durable record: the field owner's journal file must
  carry the force, release, and restore settlements attributed to the
  leg's actor beside the quality transitions and the pair's role
  records, in `seq` order — the served `GET /journal` answering the
  same record — while the standby's adopted receipt log answers the
  same settled receipts throughout.

Usage:

    force_release.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `force-release-digest <sha256>` line prints — the
check runs two passes and compares them
(`force-release-nondeterministic`). A contract violation reports
`force-release: …` lines on stderr and exits 1 — the check's
`force-release-failed`. `--tamper expect-standing` doctors the
post-release expectation to the substitution still standing — a
genuine release must fail it — and `--tamper unjournaled-release`
drops the release's settled receipt from the record the audit reads —
a journal that settled without journaling the transition must fail
the audit rather than pass silently.
"""

import argparse
import hashlib
import json
import os
import sys

import force_carryover
import pair
import simulate
import takeover


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The driven ticks each phase runs — the scans the substitution must
# stand across and the post-release scans proving it never resumes.
# The actor the leg's receipted submissions declare.
HOLD_TICKS = 3
RELEASE_TICKS = 2
ACTOR = "ci-force-release"

# The substituted stamp a forced sample carries — the served quality
# encoding of `Uncertain(Substituted)`.
SUBSTITUTED = {"uncertain": "substituted"}


def submit(url, command, failures):
    """POST one receipted command to the active's `/command` and assert
    the `accepted` submission — returns the receipt."""
    status, receipt = pair.request(
        f"{url}/command", {"command": command, "actor": ACTOR}
    )
    if status != 200 or simulate.receipt_outcome(receipt) != "accepted":
        failures.append(
            f"the receipted command {command} answered {status} "
            f"{receipt}, expected an accepted receipt"
        )
        raise Abort
    return receipt


def applied_at(receipts, command):
    """The tick `command` settled `applied` at in the adopted receipt
    log, or None."""
    for entry in receipts:
        if entry.get("command") == command:
            outcome = entry.get("outcome", {})
            if "applied" in outcome:
                return outcome["applied"]["tick"]
    return None


def identical_receipts(duty_url, standby_url, failures):
    """Assert the peers' adopted receipt logs answer identically —
    the pair's one command audit — and return the log."""
    receipts_duty = pair.get(f"{duty_url}/receipts", "GET /receipts", failures)
    receipts_standby = pair.get(
        f"{standby_url}/receipts", "GET /receipts", failures
    )
    if receipts_duty != receipts_standby:
        failures.append(
            "the peers' receipt logs diverged — the adopted audit is "
            "not one log"
        )
        raise Abort
    return receipts_duty


def journal_events(entries):
    """The leg's audit stream out of a journal entry list —
    `("settled", command, outcome, actor)` for each settled receipt,
    `("changed", point, to)` for each journaled value transition,
    `("quality", point, to)` for each quality transition, and
    `("role", from, to)` for each role change — in `seq` order."""
    events = []
    for entry in entries:
        event = entry.get("event", {})
        if "command_settled" in event:
            receipt = event["command_settled"].get("receipt", {})
            events.append(
                (
                    "settled",
                    receipt.get("command"),
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


def release_pass(args, tamper):
    """The release run: converge, control, force, hold, release,
    restore, switch, restore roles, audit. Returns `(digest_entries,
    evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the release leg "
            "has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    with open(args.model) as handle:
        model = json.load(handle)
    point = force_carryover.force_target(model)
    if point is None:
        raise Abort(
            "the emitted model declares no writable In point at "
            f"{force_carryover.FORCE_SIGNAL} — the release leg has "
            "nothing to exercise"
        )
    forced = {"bool": True}
    force_command = {
        "force_point": {"point": point, "kind": "bool", "value": forced}
    }
    unforce_command = {"unforce_point": {"point": point}}

    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared, tamper)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        duty_files, standby_files = rig.duty_files, rig.standby_files

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

        # Phase 2 — the control and the receipted force. The unforced
        # sample is the value that would otherwise have served; it must
        # differ from the forced value or the substitution proves
        # nothing.
        control = force_carryover.point_sample(owner, point)
        if control is None:
            failures.append(
                f"the force target point {point} serves no sample"
            )
            raise Abort
        if control["quality"] != "good":
            failures.append(
                f"the force target point {point} serves {control} "
                "before the force — the unforced control must read good"
            )
            raise Abort
        if control["value"] == forced:
            failures.append(
                f"the force target point {point} already reads {forced} "
                "unforced — forcing it proves nothing"
            )
            raise Abort
        receipt = submit(duty_url, force_command, failures)
        # The settling scans: the duty applies the force at its scan
        # boundary while the tracker carries its adopted receipt
        # (issue #689) — a quiesced scan must not mint an `Applied`
        # the line never ordered — one tick, same time. The
        # convergence tick then adopts the settlement, so the adopted
        # audit reads as one log below.
        tracked_settling = pair.scan(standby_url, failures)
        owner = pair.scan(duty_url, failures)
        if tracked_settling["tick"] != owner["tick"]:
            failures.append(
                "the force's settling tick advanced the peers to "
                f"{tracked_settling['tick']} and {owner['tick']}"
            )
            raise Abort
        apply_tick = owner["tick"]
        active_sample = force_carryover.assert_force(
            owner, point, forced, "the active's", failures
        )
        if failures:
            raise Abort
        tracked, owner = pair.tick(standby_url, duty_url, failures)
        tracked_sample = force_carryover.assert_force(
            tracked, point, forced, "the tracking peer's", failures
        )
        if failures:
            raise Abort
        receipts = identical_receipts(duty_url, standby_url, failures)
        if applied_at(receipts, force_command) != apply_tick:
            failures.append(
                "the force never settled applied at the applying "
                f"scan's tick {apply_tick} — the adopted receipt log "
                "does not carry the substitution's boundary"
            )
            raise Abort
        # The journaled force: the active's served journal already
        # carries the attributed settlement — the durable audit below
        # checks the file itself.
        journal = pair.get(f"{duty_url}/journal", "GET /journal", failures)
        if ("settled", force_command, "applied", ACTOR) not in (
            journal_events(journal)
        ):
            failures.append(
                "the active's served journal carries no settled "
                "applied receipt attributed to the leg's actor for "
                "the journaled force"
            )
            raise Abort
        evidence["forced_at"] = apply_tick
        digest_entries.append(
            {
                "phase": "force",
                "point": point,
                "control": control,
                "receipt": receipt,
                "apply_tick": apply_tick,
                "active": {"forces": owner["forces"], "sample": active_sample},
                "tracked": {
                    "forces": tracked["forces"],
                    "sample": tracked_sample,
                },
            }
        )

        # Phase 3 — the substitution stands across scans: each further
        # driven tick re-stamps the forced value at substituted quality
        # — the held image never leaking through the override.
        held_ticks = []
        held_sample = None
        for _ in range(HOLD_TICKS):
            tracked, owner = pair.tick(standby_url, duty_url, failures)
            held_sample = force_carryover.assert_force(
                owner, point, forced, "the active's", failures
            )
            if failures:
                raise Abort
            held_ticks.append(owner["tick"])
        evidence["held_through"] = held_ticks[-1]
        digest_entries.append(
            {"phase": "hold", "ticks": held_ticks, "sample": held_sample}
        )

        # Phase 4 — the release at its apply tick: `unforce_point`
        # through the same receipted path settles `applied` at the
        # next scan boundary, the `forces` set empties, and the point
        # resumes its unforced serve — for the internal target the
        # held-value rule leaves the force's last stamp re-stamped
        # `Good` at the boundary, the observable state a same-value
        # `write_value` produces. A release that cleared the badge
        # while the substituted value kept serving fails here.
        release = submit(duty_url, unforce_command, failures)
        # The settling scans, as in the force phase: the duty
        # releases at its boundary while the tracker carries (#689);
        # the convergence tick adopts it.
        tracked_settling = pair.scan(standby_url, failures)
        owner = pair.scan(duty_url, failures)
        if tracked_settling["tick"] != owner["tick"]:
            failures.append(
                "the release's settling tick advanced the peers to "
                f"{tracked_settling['tick']} and {owner['tick']}"
            )
            raise Abort
        apply_tick = owner["tick"]
        sample = force_carryover.point_sample(owner, point)
        if tamper == "expect-standing":
            # The doctored expectation — a release that leaves the
            # substitution standing: the badge still naming the point
            # and the sample still stamped Substituted. A genuine
            # release must fail it, naming the live reading.
            entry = force_carryover.force_entry(
                owner.get("forces", []), point
            )
            if (
                entry is None
                or entry.get("value") != forced
                or sample is None
                or sample["value"] != forced
                or sample["quality"] != SUBSTITUTED
            ):
                failures.append(
                    f"the released point reads {sample} with forces "
                    f"{owner.get('forces')}, expected the substitution "
                    f"still standing at {forced} {SUBSTITUTED}"
                )
                raise Abort
        else:
            if owner.get("forces"):
                failures.append(
                    f"the released run still carries forces "
                    f"{owner.get('forces')} — the unforce did not "
                    "empty the set"
                )
            if (
                sample is None
                or sample["value"] != forced
                or sample["quality"] != "good"
                or sample["tick"] != apply_tick
            ):
                failures.append(
                f"the released point reads {sample} at tick "
                f"{apply_tick}, expected the held value {forced} "
                "re-stamped good at the release's apply tick"
            )
        if failures:
            raise Abort
        # The convergence tick adopts the release's settlement before
        # the adopted audit is compared.
        tracked, owner = pair.tick(standby_url, duty_url, failures)
        receipts = identical_receipts(duty_url, standby_url, failures)
        if applied_at(receipts, unforce_command) != apply_tick:
            failures.append(
                "the release never settled applied at the applying "
                f"scan's tick {apply_tick} — a release that never "
                "settled is the dishonesty this leg exists to name"
            )
            raise Abort
        # The release stands: further scans leave the held image
        # stamped at the boundary — never re-substituting.
        released_ticks = [apply_tick]
        for _ in range(RELEASE_TICKS):
            tracked, owner = pair.tick(standby_url, duty_url, failures)
            sample = force_carryover.point_sample(owner, point)
            if (
                sample is None
                or sample["value"] != forced
                or sample["quality"] != "good"
                or sample["tick"] != apply_tick
            ):
                failures.append(
                    f"the released point re-stamped to {sample} at "
                    f"tick {owner['tick']} — the release did not hold"
                )
                raise Abort
            if owner.get("forces"):
                failures.append(
                    f"the released run regained forces "
                    f"{owner.get('forces')} at tick {owner['tick']}"
                )
                raise Abort
            released_ticks.append(owner["tick"])
        evidence["released_at"] = apply_tick
        digest_entries.append(
            {
                "phase": "release",
                "receipt": release,
                "apply_tick": apply_tick,
                "ticks": released_ticks,
                "forces": owner["forces"],
                "sample": sample,
            }
        )

        # Phase 5 — the live path proven: a receipted `write_value`
        # returns the pre-force held value — the point serving the
        # written value at `Good`, the substitution's image no longer
        # standing between the operator and the point.
        write_command = takeover.write_value(point, control["value"]["bool"])
        receipt = submit(duty_url, write_command, failures)
        # The settling scans, as in the force phase: the duty applies
        # the restore write while the tracker carries (#689); the
        # convergence tick adopts it before the audit is compared.
        tracked_settling = pair.scan(standby_url, failures)
        owner = pair.scan(duty_url, failures)
        if tracked_settling["tick"] != owner["tick"]:
            failures.append(
                "the restore write's settling tick advanced the peers "
                f"to {tracked_settling['tick']} and {owner['tick']}"
            )
            raise Abort
        apply_tick = owner["tick"]
        sample = force_carryover.point_sample(owner, point)
        if (
            sample is None
            or sample["value"] != control["value"]
            or sample["quality"] != "good"
        ):
            failures.append(
                f"the restore write left the point reading {sample}, "
                f"expected the pre-force held value {control['value']} "
                "at good — the live write path did not resume"
            )
            raise Abort
        tracked, owner = pair.tick(standby_url, duty_url, failures)
        receipts = identical_receipts(duty_url, standby_url, failures)
        if applied_at(receipts, write_command) != apply_tick:
            failures.append(
                "the restore write never settled applied at the "
                f"applying scan's tick {apply_tick}"
            )
        if failures:
            raise Abort
        evidence["restored_at"] = apply_tick
        digest_entries.append(
            {
                "phase": "restore-write",
                "receipt": receipt,
                "apply_tick": apply_tick,
                "sample": sample,
            }
        )

        # Phase 6 — the released state is adopted run state: the
        # documented switch makes the tracking peer field owner, and
        # the promoted peer must serve what the checkpoint carried —
        # no resurrected force, the live image standing.
        switched = rig.switch(
            duty_url, standby_url, failures, audit_receipts=True
        )
        owner = switched["owner"]
        sample = force_carryover.point_sample(owner, point)
        if owner.get("forces"):
            failures.append(
                f"the promoted peer resurrected forces "
                f"{owner.get('forces')} — a released force rode the "
                "promotion back in"
            )
        if (
            sample is None
            or sample["value"] != control["value"]
            or sample["quality"] != "good"
        ):
            failures.append(
                f"the promoted peer's point reads {sample}, expected "
                f"the carried live image {control['value']} at good"
            )
        if not all(
            takeover.settled(switched["receipts"], command)
            for command in (force_command, unforce_command, write_command)
        ):
            failures.append(
                "the promoted peer's adopted log lost a settled "
                "receipt — the pair's one audit is not carried"
            )
        if failures:
            raise Abort
        evidence["switched_at"] = switched["demote"]["tick"]
        digest_entries.append(
            {
                "phase": "switch",
                "demote": switched["demote"],
                "promote": switched["promote"],
                "ticks": switched["ticks"],
                "forces": owner["forces"],
                "sample": sample,
            }
        )

        # Phase 7 — the launch roles restored: demote the new owner,
        # promote the reconverged peer, leaving the manifest-declared
        # duty controller `active` and its standby `tracking`.
        restored = rig.switch(
            standby_url,
            duty_url,
            failures,
            demote_what="the new field owner",
            promote_what="the reconverged peer",
            audit_receipts=True,
        )
        owner = restored["owner"]
        evidence["roles_restored_at"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "restore-roles",
                "demote": restored["demote"],
                "promote": restored["promote"],
                "ticks": restored["ticks"],
                "duty_role": restored["promoted_role"],
                "standby_role": restored["demoted_role"],
            }
        )

        # Phase 8 — the durable record: each peer's journal file
        # carries the run's records in `seq` order — the field owner's
        # holding the force, release, and restore settlements
        # attributed to the leg's actor beside the quality transitions
        # and the pair's role records; the standby's adopted receipt
        # log answered the same settled receipts throughout, asserted
        # phase by phase above.
        files = {
            duty_decl["name"]: duty_files,
            standby_decl["name"]: standby_files,
        }
        want_roles = {
            duty_decl["name"]: [
                ("active", "demoting"),
                ("demoting", "standby"),
                ("standby", "promoting"),
                ("promoting", "active"),
            ],
            standby_decl["name"]: [
                ("standby", "promoting"),
                ("promoting", "active"),
                ("active", "demoting"),
                ("demoting", "standby"),
            ],
        }
        persisted = {}
        events = None
        for name in (duty_decl["name"], standby_decl["name"]):
            journal_path = files[name].get("journal_file")
            if journal_path is None or not os.path.exists(journal_path):
                failures.append(
                    f"{name}'s declared journal file does not exist "
                    "— the --journal-file flag was not honored"
                )
                continue
            records = pair.journal_records(journal_path)
            boundaries = [
                record for kind, record in records if kind == "boundary"
            ]
            if boundaries != [{"run": 1, "tick": 0}]:
                failures.append(
                    f"{name}'s journal boundaries are {boundaries}, "
                    "expected the single cold-start marker"
                )
            entries = [
                record for kind, record in records if kind == "entry"
            ]
            seqs = [entry["seq"] for entry in entries]
            if seqs != list(range(1, len(seqs) + 1)):
                failures.append(
                    f"{name}'s journal seqs are not 1..n in order: {seqs}"
                )
            transitions = pair.role_transitions(entries)
            if [
                (frm, to) for _tick, frm, to in transitions
            ] != want_roles[name]:
                failures.append(
                    f"{name}'s journal file carries the role "
                    f"transitions {transitions}, expected "
                    f"{want_roles[name]}"
                )
            persisted[name] = {
                "journal_records": records,
                "role_transitions": transitions,
            }
            if name == duty_decl["name"]:
                events = journal_events(entries)
        if failures:
            raise Abort
        served = pair.get(f"{duty_url}/journal", "GET /journal", failures)
        if journal_events(served) != events:
            failures.append(
                "the served journal's transition stream diverges from "
                "the durable file's — the monitor does not answer the "
                "record it persists"
            )
            raise Abort

        # The doctored record the tamper exercises: a journal that
        # settled the release without journaling the transition — the
        # audit must name the missing settlement, never pass it
        # silently.
        if tamper == "unjournaled-release":
            events.remove(
                ("settled", unforce_command, "applied", ACTOR)
            )

        groups = [
            # The force: the attributed settlement and the substituted
            # stamp it produced.
            [
                ("settled", force_command, "applied", ACTOR),
                ("quality", point, SUBSTITUTED),
            ],
            # The release: the attributed settlement and the
            # re-stamped live quality.
            [
                ("settled", unforce_command, "applied", ACTOR),
                ("quality", point, "good"),
            ],
            # The restore write's settlement.
            [
                ("settled", write_command, "applied", ACTOR),
            ],
            # The switch and the restored launch roles.
            [
                ("role", "active", "demoting"),
                ("role", "demoting", "standby"),
            ],
            [
                ("role", "standby", "promoting"),
                ("role", "promoting", "active"),
            ],
        ]
        cursor = 0
        for index, group in enumerate(groups):
            positions = []
            for want in group:
                position = next(
                    (
                        at
                        for at in range(cursor, len(events))
                        if events[at] == want
                    ),
                    None,
                )
                if position is None:
                    failures.append(
                        f"the durable journal carries no {want} at or "
                        f"after group {index}'s position — the "
                        "transition is missing or out of order"
                    )
                else:
                    positions.append(position)
            if positions:
                cursor = max(positions) + 1
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "audit",
                "persisted": persisted,
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
        choices=["expect-standing", "unjournaled-release"],
        help="doctor the leg's own expectations — the pass must fail "
        "naming the evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = release_pass(args, args.tamper)
    except Abort as abort:
        for line in abort.args:
            eprint(f"force-release: {line}")
        return 1
    for failure in failures:
        eprint(f"force-release: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"force-release: the {args.tamper} case passed "
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
        f"force-release-digest {digest} — tracking by tick "
        f"{evidence['converged']}, forced at tick "
        f"{evidence['forced_at']}, held through tick "
        f"{evidence['held_through']}, released at tick "
        f"{evidence['released_at']}, restored at tick "
        f"{evidence['restored_at']}, roles restored at tick "
        f"{evidence['roles_restored_at']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
