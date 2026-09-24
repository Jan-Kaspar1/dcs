#!/usr/bin/env python3
"""The stale-checkpoint leg for the reference plant — the consumer-side
proof that a receipted `unforce_point` on the deployed redundant pair
is durable truth a tracking peer's re-adoption of a staler checkpoint
image cannot silently revert (WW-ENG-003, WW-LCM-001 — the
receipted-command contract's adoption rule).

The force-carryover leg (`ci/force_carryover.py`) proves a standing
force rides the promotion; the force-release leg (`ci/force_release.py`)
proves the receipted release half. This leg proves the adoption half
the release leg's switch leaves unproven: the defect a restarting
standby demonstrated, adopting a staler peer's image and silently
reverting an unforce its own journal had receipted two ticks earlier —
the resurrected force then propagating pair-wide with no receipt and
no audit. The rig reads the standby wiring and persistence fields out
of `deploy/manifest.json` and spawns the released tooling exactly as
the pair legs do. The run:

- converges the declared standby to `tracking` through the pair leg's
  driven-tick loop, then records the force target's unforced sample —
  the control proving the unforced value is what would otherwise have
  served, which must differ from the forced value;
- submits `force_point` through `POST /command` on the active's
  monitor — the emitted model marks only *internal* `In` points
  writable, so the leg's `p101-hand` is the honest target per the
  force-carryover convention, inert downstream while the pump stands
  in auto — asserting the `accepted` submission, the `applied`
  settlement identical in both peers' adopted log at the apply
  boundary's tick, the snapshot's `forces` entry and the
  `Uncertain(Substituted)` sample on both peers, and the active's
  served journal carrying the attributed settlement;
- issues the documented switch — `POST /demote` on the field owner,
  `POST /promote` on the converged standby — asserting the promoted
  peer still carries the force: the `forces` entry naming the point
  and the forced value at substituted quality across scans;
- submits `unforce_point` through the new active's receipted path,
  asserting the release at its apply tick: the `forces` set empty,
  the point resuming its unforced serve — for the internal target the
  held-value rule leaves the force's last stamp re-stamped `Good` —
  on both peers, the `applied` settlement landing at that scan
  boundary in the identical adopted log, and the journaled release on
  both peers' served records;
- restarts the tracking peer onto its declared
  `--state-file`/`--journal-file` — the field owner driven through
  the downtime with its writes landing — the resumed peer reporting
  `standby` unsynchronized rather than claiming the field,
  reconverging to `tracking` inside the declared window, its journal
  file carrying the restart boundary ordered after run 1's entries
  with `seq` order intact: the re-adoption per the reproduction
  shape, the restarted image staler than the owner's run;
- asserts the force does not re-stand across the re-adoption: the
  served `forces` stays empty, the point's live value keeps serving
  unforced at `Good`, the adopted receipt log preserves the unforce
  settlement exactly once, and no phantom force receipt — no
  post-release `force_point` settlement and no checkpoint-attributed
  adoption receipt — appears on either peer's served or durable
  journal;
- restores the pair's launch roles — `demote` on the new owner,
  `promote` on the reconverged peer — leaving the manifest-declared
  duty controller `active` and its standby `tracking`, and audits the
  durable record in `seq` order.

Usage:

    stale_checkpoint.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `stale-checkpoint-digest <sha256>` line prints — the
check runs two passes and compares them
(`stale-checkpoint-nondeterministic`). A contract violation reports
`stale-checkpoint: …` lines on stderr and exits 1 — the check's
`stale-checkpoint-failed`. `--tamper expect-standing` doctors the
post-re-adoption expectation to the substitution still standing — a
genuine release-keeping run must fail it naming the live reading.
"""

import argparse
import hashlib
import json
import os
import re
import sys

import force_carryover
import pair
import simulate
import takeover


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The driven ticks each window runs — the post-switch scans the force
# must stand across, the post-release scans proving it never resumes,
# the downtime the field owner keeps writing through while its peer is
# dead, and the declared reconvergence window the resumed peer must
# report tracking inside. The actor the leg's receipted submissions
# declare.
CARRY_TICKS = 2
RELEASE_TICKS = 2
DOWNTIME_TICKS = 3
RECONVERGE_TICKS = 4
ACTOR = "ci-stale-checkpoint"

# The substituted stamp a forced sample carries — the served quality
# encoding of `Uncertain(Substituted)`.
SUBSTITUTED = {"uncertain": "substituted"}


def submit(url, command, failures):
    """POST one receipted command to the owner's `/command` and assert
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


def count_settled(receipts, command):
    """The `applied` settlements one command carries in a receipt log."""
    return sum(
        1
        for entry in receipts
        if entry.get("command") == command
        and simulate.receipt_outcome(entry) == "applied"
    )


def identical_receipts(first_url, second_url, failures):
    """Assert the peers' adopted receipt logs answer identically —
    the pair's one command audit — and return the log."""
    first = pair.get(f"{first_url}/receipts", "GET /receipts", failures)
    second = pair.get(f"{second_url}/receipts", "GET /receipts", failures)
    if first != second:
        failures.append(
            "the peers' receipt logs diverged — the adopted audit is "
            "not one log"
        )
        raise Abort
    return first


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


def settled_entries(entries):
    """The `(tick, command, actor)` stream of `command_settled` journal
    entries — the phantom-force audit's input."""
    settled = []
    for entry in entries:
        event = entry.get("event", {})
        if "command_settled" in event:
            receipt = event["command_settled"].get("receipt", {})
            outcome = receipt.get("outcome", {})
            tick = outcome.get("applied", {}).get("tick")
            settled.append((tick, receipt.get("command"), receipt.get("actor")))
    return settled


def assert_phantom_free(entries, force_command, unforce_command, release_tick,
                        point, where, failures):
    """Assert no phantom force receipt haunts a journal entry list: no
    `force_point` settlement past the release's apply tick — the
    resurrected force would settle or re-settle there — and no
    checkpoint-attributed adoption receipt for the leg's point, the
    audit the adoption rule journals only for changes no receipt
    backs."""
    for tick, command, actor in settled_entries(entries):
        if command == force_command and tick is not None and tick > release_tick:
            failures.append(
                f"{where} carries a phantom force settlement at tick "
                f"{tick} past the release at tick {release_tick} — the "
                "released force re-stood with a receipt"
            )
        if (
            isinstance(command, dict)
            and ("force_point" in command or "unforce_point" in command)
            and command.get("force_point", command.get("unforce_point", {})).get(
                "point"
            ) == point
            and isinstance(actor, str)
            and actor.startswith("checkpoint")
        ):
            failures.append(
                f"{where} carries a checkpoint-attributed adoption "
                f"receipt {command} — the re-adoption authored a force "
                "change no settled receipt backs"
            )
    _ = unforce_command


def assert_released(snapshot, point, forced, release_tick, what, failures):
    """The released-state assertions on one snapshot: the `forces` set
    empty and the point serving the held value re-stamped `Good` at
    the release's apply tick. Returns the fetched sample, or None
    after recording a failure."""
    if snapshot.get("forces"):
        failures.append(
            f"{what} still carries forces {snapshot.get('forces')} — "
            "the released force re-stood on the re-adoption"
        )
        return None
    sample = force_carryover.point_sample(snapshot, point)
    if (
        sample is None
        or sample["value"] != forced
        or sample["quality"] != "good"
        or sample["tick"] != release_tick
    ):
        failures.append(
            f"{what} reads {sample} at tick {snapshot.get('tick')}, "
            f"expected the held value {forced} re-stamped good at the "
            f"release's apply tick {release_tick} — the live value "
            "stopped serving unforced"
        )
        return None
    return sample


def role(url, failures):
    """The peer's served RoleReport."""
    return pair.get(f"{url}/role", "GET /role", failures)


def tracking(report):
    """Whether a RoleReport reads `standby` under `tracking` sync."""
    sync = report.get("sync")
    return report.get("role") == "standby" and (
        isinstance(sync, dict) and "tracking" in sync
    )


def stale_checkpoint_pass(args, tamper):
    """The stale-checkpoint run: converge, force, switch, release,
    restart the tracker, re-adopt, restore roles, audit. Returns
    `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "stale-checkpoint leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    with open(args.model) as handle:
        model = json.load(handle)
    point = force_carryover.force_target(model)
    if point is None:
        raise Abort(
            "the emitted model declares no writable In point at "
            f"{force_carryover.FORCE_SIGNAL} — the stale-checkpoint "
            "leg has nothing to exercise"
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
        # boundary while the tracker carries its adopted receipt — a
        # quiesced scan must not mint an `Applied` the line never
        # ordered — one tick, same time. The convergence tick then
        # adopts the settlement, so the adopted audit reads as one log
        # below.
        tracked_settling = pair.scan(standby_url, failures)
        owner = pair.scan(duty_url, failures)
        if tracked_settling["tick"] != owner["tick"]:
            failures.append(
                "the force's settling tick advanced the peers to "
                f"{tracked_settling['tick']} and {owner['tick']}"
            )
            raise Abort
        force_tick = owner["tick"]
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
        if applied_at(receipts, force_command) != force_tick:
            failures.append(
                "the force never settled applied at the applying "
                f"scan's tick {force_tick} — the adopted receipt log "
                "does not carry the substitution's boundary"
            )
            raise Abort
        journal = pair.get(f"{duty_url}/journal", "GET /journal", failures)
        if ("settled", force_command, "applied", ACTOR) not in journal_events(
            journal
        ):
            failures.append(
                "the active's served journal carries no settled "
                "applied receipt attributed to the leg's actor for "
                "the journaled force"
            )
            raise Abort
        evidence["forced_at"] = force_tick
        digest_entries.append(
            {
                "phase": "force",
                "point": point,
                "control": control,
                "receipt": receipt,
                "apply_tick": force_tick,
                "active": {"forces": owner["forces"], "sample": active_sample},
                "tracked": {
                    "forces": tracked["forces"],
                    "sample": tracked_sample,
                },
            }
        )

        # Phase 3 — the documented switch: demote the field owner,
        # promote the converged standby, each answered by its
        # RoleReport. The handover ticks keep the peers identical;
        # the promoted peer must still carry the force the checkpoint
        # rode in on.
        switched = rig.switch(
            duty_url, standby_url, failures, audit_receipts=True
        )
        owner = switched["owner"]
        carried_sample = force_carryover.assert_force(
            owner, point, forced, "the promoted peer's", failures
        )
        if failures:
            raise Abort
        carry_ticks = [owner["tick"]]
        for _ in range(CARRY_TICKS):
            tracked, owner = pair.tick(duty_url, standby_url, failures)
            sample = force_carryover.assert_force(
                owner, point, forced, "the promoted peer's", failures
            )
            if failures:
                raise Abort
            carried_sample = sample
            carry_ticks.append(owner["tick"])
        if not takeover.settled(switched["receipts"], force_command):
            failures.append(
                "the promoted peer's adopted log lost the force's "
                "settled receipt — the pair's one audit is not carried"
            )
            raise Abort
        evidence["switched_at"] = switched["demote"]["tick"]
        digest_entries.append(
            {
                "phase": "switch",
                "demote": switched["demote"],
                "promote": switched["promote"],
                "ticks": switched["ticks"] + carry_ticks[1:],
                "forces": owner["forces"],
                "sample": carried_sample,
            }
        )

        # Phase 4 — the release on the new active: `unforce_point`
        # through the same receipted path settles `applied` at the
        # next scan boundary, the `forces` set empties, and the point
        # resumes its unforced serve — for the internal target the
        # held-value rule leaves the force's last stamp re-stamped
        # `Good`.
        release = submit(standby_url, unforce_command, failures)
        # The settling scans, as in the force phase: the owner
        # releases at its boundary while the tracker carries; the
        # convergence tick adopts it.
        tracked_settling = pair.scan(duty_url, failures)
        owner = pair.scan(standby_url, failures)
        if tracked_settling["tick"] != owner["tick"]:
            failures.append(
                "the release's settling tick advanced the peers to "
                f"{tracked_settling['tick']} and {owner['tick']}"
            )
            raise Abort
        release_tick = owner["tick"]
        sample = assert_released(
            owner, point, forced, release_tick, "the new active's", failures
        )
        if failures:
            raise Abort
        tracked, owner = pair.tick(duty_url, standby_url, failures)
        receipts = identical_receipts(standby_url, duty_url, failures)
        if applied_at(receipts, unforce_command) != release_tick:
            failures.append(
                "the release never settled applied at the applying "
                f"scan's tick {release_tick} — a release that never "
                "settled is the dishonesty this leg exists to name"
            )
            raise Abort
        released_ticks = [release_tick]
        for _ in range(RELEASE_TICKS):
            tracked, owner = pair.tick(duty_url, standby_url, failures)
            sample = assert_released(
                owner, point, forced, release_tick, "the released", failures
            )
            if failures:
                raise Abort
            released_ticks.append(owner["tick"])
        # The journaled release on both peers' served records — the
        # durable truth the re-adoption must not revert.
        for url, what in (
            (standby_url, "the new active's"),
            (duty_url, "the tracking peer's"),
        ):
            journal = pair.get(f"{url}/journal", "GET /journal", failures)
            if ("settled", unforce_command, "applied", ACTOR) not in (
                journal_events(journal)
            ):
                failures.append(
                    f"{what} served journal carries no settled applied "
                    "receipt attributed to the leg's actor for the "
                    "journaled release"
                )
            assert_phantom_free(
                journal, force_command, unforce_command, release_tick,
                point, f"{what} served journal", failures,
            )
        if failures:
            raise Abort
        evidence["released_at"] = release_tick
        digest_entries.append(
            {
                "phase": "release",
                "receipt": release,
                "apply_tick": release_tick,
                "ticks": released_ticks,
                "forces": owner["forces"],
                "sample": sample,
            }
        )

        # Phase 5 — the restart: the tracking peer's container stops
        # while the field owner keeps running. The declared files must
        # hold the tracking run's checkpoint and run-1 record — the
        # receipted release among the journaled entries the restart
        # boundary must order after.
        state_file = duty_files.get("state_file")
        journal_file = duty_files.get("journal_file")
        if state_file is None or journal_file is None:
            raise Abort(
                "the manifest's duty declares no "
                "state_file/journal_file — the stale-checkpoint leg "
                "has nothing to restart onto"
            )
        stopped = pair.get(
            f"{duty_url}/snapshot", "GET /snapshot", failures
        )
        served_tracking = pair.get(
            f"{duty_url}/journal", "GET /journal", failures
        )
        served_owner = pair.get(
            f"{standby_url}/journal", "GET /journal", failures
        )
        pair.stop(rig.duty)
        if not os.path.exists(state_file):
            failures.append(
                f"the tracking run left no state file at {state_file}"
            )
            raise Abort
        try:
            with open(state_file) as handle:
                checkpoint = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            failures.append(
                f"the tracker's persisted state file does not parse: {error}"
            )
            raise Abort
        persisted = checkpoint.get("tick")
        evidence["persisted_tick"] = persisted
        if persisted != stopped["tick"]:
            failures.append(
                f"the tracker's state file persisted tick {persisted} "
                f"while the tracking run stood at {stopped['tick']}"
            )
            raise Abort
        if checkpoint.get("model_fingerprint") != rig.fingerprint:
            failures.append(
                "the tracker's state file carries fingerprint "
                f"{checkpoint.get('model_fingerprint')}, the manifest "
                f"declares {rig.fingerprint}"
            )
            raise Abort
        records = pair.journal_records(journal_file)
        boundaries = [
            record for kind, record in records if kind == "boundary"
        ]
        if boundaries != [{"run": 1, "tick": 0}]:
            failures.append(
                f"run 1's journal boundaries are {boundaries}, "
                "expected the single cold-start marker"
            )
            raise Abort
        run1_entries = [
            record for kind, record in records if kind == "entry"
        ]
        seqs = [entry["seq"] for entry in run1_entries]
        if seqs != list(range(1, len(seqs) + 1)):
            failures.append(
                f"run 1's journal seqs are not 1..n in order: {seqs}"
            )
            raise Abort
        if not any(
            "command_settled" in entry.get("event", {})
            for entry in run1_entries
        ):
            failures.append(
                "the tracker's run-1 journal carries no settled "
                "receipt — the restart point precedes the entries the "
                "boundary must order after"
            )
            raise Abort
        evidence["run1_entries"] = len(run1_entries)

        # Phase 6 — the downtime window: the tracker dead, the field
        # owner's driven scans keep writing the field — the run's tick
        # advancing past the persisted image, so the resumed peer
        # re-adopts a newer checkpoint per the reproduction shape.
        for _ in range(DOWNTIME_TICKS):
            owner = pair.scan(standby_url, failures)
        duty_role = role(standby_url, failures)
        if duty_role.get("role") != "active":
            failures.append(
                f"the field owner reports {duty_role.get('role')!r} "
                "after the downtime window, expected active — the "
                "tracker's death disturbed it"
            )
            raise Abort
        digest_entries.append(
            {"phase": "downtime", "tick": owner["tick"]}
        )

        # Phase 7 — the relaunch onto the declared files: the preamble
        # must report the resume at the persisted tick, the served
        # role surface must read `standby` unsynchronized — the
        # resumed peer rejoins in standby, never claiming the field —
        # and the durable file carries the run-2 boundary ordered
        # after run 1's entries with `seq` order intact. The relaunch
        # rebinds the duty's original monitor address: the manifest's
        # standby wiring names the duty by address, so a new ephemeral
        # port would strand the flag-armed peer on the dead address at
        # the restore switch — the demoted peer degrading on a refused
        # fetch instead of reconverging tracking.
        old_hostport = duty_url.removeprefix("http://")
        old_host, _, old_port = old_hostport.rpartition(":")
        rig.duty, duty_url, preamble = pair.spawn_peer(
            args.controller,
            args.model,
            args.dt,
            rig.plant_addr,
            standby_url.removeprefix("http://"),
            duty_files,
            listen=f"{old_host}:{old_port}",
            pair_token=pair.PAIR_TOKEN,
        )
        rig.duty_url = duty_url
        if duty_url is None:
            detail = "; ".join(preamble[-2:]) or "no diagnostic"
            failures.append(
                f"the relaunched tracker exited at startup: {detail}"
            )
            raise Abort
        line = next(
            (entry for entry in preamble if "resumed from state file" in entry),
            None,
        )
        if line is None:
            failures.append(
                "the relaunched tracker never reported a resume — its "
                "cold start at tick 0 silently abandons the persisted "
                f"run at tick {persisted}"
            )
            raise Abort
        match = re.search(r"at tick (\d+)", line)
        resumed = int(match.group(1)) if match else None
        evidence["resumed_tick"] = resumed
        if resumed != persisted:
            failures.append(
                f"the relaunch resumed at tick {resumed}, the state "
                f"file persisted {persisted}"
            )
            raise Abort
        snapshot = pair.get(
            f"{duty_url}/snapshot", "GET /snapshot", failures
        )
        if snapshot["tick"] != persisted:
            failures.append(
                f"the resumed tracker reports tick {snapshot['tick']}, "
                f"the persisted tick is {persisted}"
            )
            raise Abort
        tracker_role = role(duty_url, failures)
        if tracker_role.get("role") != "standby" or (
            tracker_role.get("sync") != "unsynchronized"
        ):
            failures.append(
                f"the resumed tracker reports {tracker_role} — "
                "expected standby/unsynchronized: it must rejoin in "
                "standby, never claiming the field"
            )
            raise Abort
        duty_role = role(standby_url, failures)
        if duty_role.get("role") != "active":
            failures.append(
                f"the field owner reports {duty_role.get('role')!r} "
                "after the tracker's relaunch, expected active — the "
                "resumed peer claimed the field"
            )
            raise Abort
        replayed = pair.get(
            f"{duty_url}/journal", "GET /journal", failures
        )
        if replayed[: len(served_tracking)] != served_tracking:
            failures.append(
                "the tracker's replayed journal no longer answers the "
                "pre-restart entries verbatim"
            )
            raise Abort
        boundary = {
            "seq": len(served_tracking) + 1,
            "tick": persisted,
            "event": {"run_boundary": {"run": 2}},
        }
        if replayed[len(served_tracking):] != [boundary]:
            failures.append(
                "the resumed tracker's served journal does not open "
                f"with the run-2 boundary entry {boundary}: "
                f"{replayed[len(served_tracking):]}"
            )
            raise Abort
        records = pair.journal_records(journal_file)
        boundaries = [
            record for kind, record in records if kind == "boundary"
        ]
        if boundaries != [
            {"run": 1, "tick": 0},
            {"run": 2, "tick": persisted},
        ]:
            failures.append(
                f"the tracker's journal boundaries are {boundaries}, "
                "expected run 1 at tick 0 and run 2 at the persisted "
                f"tick {persisted}"
            )
            raise Abort
        restart_at = next(
            index
            for index, (kind, record) in enumerate(records)
            if kind == "boundary" and record.get("run") == 2
        )
        if restart_at != len(run1_entries) + 1:
            failures.append(
                "the tracker's restart boundary is not ordered after "
                "the pre-restart run's entries"
            )
            raise Abort
        seqs = [
            record["seq"]
            for kind, record in records
            if kind == "entry"
        ]
        if seqs != list(range(1, len(seqs) + 1)):
            failures.append(
                "the tracker's journal seqs do not continue 1..n "
                f"across the restart boundary: {seqs}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "restart",
                "persisted_tick": persisted,
                "resumed_tick": resumed,
                "tracker_role": tracker_role,
                "boundaries": boundaries,
            }
        )

        # Phase 8 — the re-adoption: driven tracking-first ticks until
        # the resumed peer reports `tracking` inside the declared
        # window — and the force must not re-stand on either peer.
        reconverged = None
        ticks = []
        readopt_sample = None
        for _ in range(RECONVERGE_TICKS):
            tracked, owner = pair.tick(
                duty_url,
                standby_url,
                failures,
                diverged="the resumed tracker's image diverged from "
                "the field owner's at tick {tick} — the restart's "
                "re-pull never realigned the pair",
            )
            ticks.append(owner["tick"])
            if tamper == "expect-standing":
                # The doctored expectation — a re-adoption that
                # resurrects the released force: the badge still
                # naming the point and the sample still stamped
                # Substituted. A genuine release-keeping run must
                # fail it, naming the live reading.
                entry = force_carryover.force_entry(
                    owner.get("forces", []), point
                )
                sample = force_carryover.point_sample(owner, point)
                if (
                    entry is None
                    or entry.get("value") != forced
                    or sample is None
                    or sample["value"] != forced
                    or sample["quality"] != SUBSTITUTED
                ):
                    failures.append(
                        f"the re-adopted point reads {sample} with "
                        f"forces {owner.get('forces')}, expected the "
                        f"substitution still standing at {forced} "
                        f"{SUBSTITUTED}"
                    )
                    raise Abort
                readopt_sample = sample
            else:
                sample = assert_released(
                    owner, point, forced, release_tick,
                    "the re-adopted owner's", failures,
                )
                if failures:
                    raise Abort
                readopt_sample = sample
                tracked_sample = assert_released(
                    tracked, point, forced, release_tick,
                    "the re-adopted tracker's", failures,
                )
                if failures:
                    raise Abort
            tracker_role = role(duty_url, failures)
            if tracking(tracker_role):
                reconverged = tracker_role
                break
        if failures:
            raise Abort
        if reconverged is None:
            failures.append(
                "the resumed tracker never reconverged to tracking "
                f"inside the declared {RECONVERGE_TICKS}-tick window — "
                f"GET /role answers {tracker_role}"
            )
            raise Abort
        # The adopted receipt log preserves the unforce settlement
        # exactly once on both peers — replayed, never re-settled —
        # and neither peer's served journal journals a phantom.
        receipts_owner = pair.get(
            f"{standby_url}/receipts", "GET /receipts", failures
        )
        receipts_tracker = pair.get(
            f"{duty_url}/receipts", "GET /receipts", failures
        )
        if receipts_owner != receipts_tracker:
            failures.append(
                "the re-adopted tracker's receipt log diverged from "
                "the field owner's — the re-pull did not restore one "
                "log"
            )
            raise Abort
        if count_settled(receipts_owner, unforce_command) != 1:
            failures.append(
                "the adopted receipt log carries the unforce "
                f"settlement {count_settled(receipts_owner, unforce_command)} "
                "times, expected exactly once — the release was "
                "re-settled or lost across the re-adoption"
            )
            raise Abort
        if count_settled(receipts_owner, force_command) != 1:
            failures.append(
                "the adopted receipt log carries the force settlement "
                f"{count_settled(receipts_owner, force_command)} times, "
                "expected exactly once"
            )
            raise Abort
        for url, what in (
            (standby_url, "the field owner's"),
            (duty_url, "the re-adopted tracker's"),
        ):
            served = pair.get(f"{url}/journal", "GET /journal", failures)
            if ("settled", unforce_command, "applied", ACTOR) not in (
                journal_events(served)
            ):
                failures.append(
                    f"{what} served journal lost the journaled release "
                    "across the re-adoption"
                )
            assert_phantom_free(
                served, force_command, unforce_command, release_tick,
                point, f"{what} served journal", failures,
            )
        if failures:
            raise Abort
        evidence["reconverged"] = ticks[-1]
        digest_entries.append(
            {
                "phase": "readopt",
                "ticks": ticks,
                "tracker_role": reconverged,
                "forces": owner["forces"],
                "sample": readopt_sample,
                "receipts": receipts_owner,
            }
        )

        # Phase 9 — the launch roles restored: demote the new owner,
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
        sample = assert_released(
            owner, point, forced, release_tick, "the restored", failures
        )
        if failures:
            raise Abort
        evidence["roles_restored_at"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "restore-roles",
                "demote": restored["demote"],
                "promote": restored["promote"],
                "ticks": restored["ticks"],
                "duty_role": restored["promoted_role"],
                "standby_role": restored["demoted_role"],
                "sample": sample,
            }
        )

        # Phase 10 — the durable record: each peer's journal file
        # carries the run's records in `seq` order — the force, the
        # journaled release, and the pair's role records attributed in
        # order, the tracker's restart boundary ordered after run 1's
        # entries — the served journals answering the same record —
        # with no phantom force settlement on either file.
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
        want_boundaries = {
            duty_decl["name"]: [
                {"run": 1, "tick": 0},
                {"run": 2, "tick": persisted},
            ],
            standby_decl["name"]: [{"run": 1, "tick": 0}],
        }
        persisted_records = {}
        events_by_name = {}
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
            if boundaries != want_boundaries[name]:
                failures.append(
                    f"{name}'s journal boundaries are {boundaries}, "
                    f"expected {want_boundaries[name]}"
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
            assert_phantom_free(
                entries, force_command, unforce_command, release_tick,
                point, f"{name}'s durable journal", failures,
            )
            persisted_records[name] = {
                "journal_records": records,
                "role_transitions": transitions,
            }
            events_by_name[name] = journal_events(entries)
        if failures:
            raise Abort
        # The served journals answer the durable files' transition
        # streams — each monitor answers the record it persists.
        for url, name in (
            (duty_url, duty_decl["name"]),
            (standby_url, standby_decl["name"]),
        ):
            served = pair.get(f"{url}/journal", "GET /journal", failures)
            if journal_events(served) != events_by_name[name]:
                failures.append(
                    f"{name}'s served journal transition stream "
                    "diverges from its durable file's — the monitor "
                    "does not answer the record it persists"
                )
                raise Abort
        # The ordered groups read per peer — the release lands on the
        # new active first and reaches the tracker through the switch:
        # the duty demotes before it adopts the release, the standby
        # promotes before it settles it. Either order still carries
        # the release riding the checkpoint, never resurrected.
        groups_by_name = {
            duty_decl["name"]: [
                # The force: the attributed settlement and the
                # substituted stamp it produced.
                [
                    ("settled", force_command, "applied", ACTOR),
                    ("quality", point, SUBSTITUTED),
                ],
                # The switch demoting the duty.
                [
                    ("role", "active", "demoting"),
                    ("role", "demoting", "standby"),
                ],
                # The adopted release and the re-stamped live quality.
                [
                    ("settled", unforce_command, "applied", ACTOR),
                    ("quality", point, "good"),
                ],
                # The restore promoting the duty.
                [
                    ("role", "standby", "promoting"),
                    ("role", "promoting", "active"),
                ],
            ],
            standby_decl["name"]: [
                [
                    ("settled", force_command, "applied", ACTOR),
                    ("quality", point, SUBSTITUTED),
                ],
                # The switch promoting the standby.
                [
                    ("role", "standby", "promoting"),
                    ("role", "promoting", "active"),
                ],
                # The settled release and the re-stamped live quality.
                [
                    ("settled", unforce_command, "applied", ACTOR),
                    ("quality", point, "good"),
                ],
                # The restore demoting the standby.
                [
                    ("role", "active", "demoting"),
                    ("role", "demoting", "standby"),
                ],
            ],
        }
        events = events_by_name[duty_decl["name"]]
        for name in (duty_decl["name"], standby_decl["name"]):
            stream = events_by_name[name]
            cursor = 0
            for index, group in enumerate(groups_by_name[name]):
                positions = []
                for want in group:
                    position = next(
                        (
                            at
                            for at in range(cursor, len(stream))
                            if stream[at] == want
                        ),
                        None,
                    )
                    if position is None:
                        failures.append(
                            f"{name}'s durable journal carries no "
                            f"{want} at or after group {index}'s "
                            "position — the transition is missing or "
                            "out of order"
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
                "persisted": persisted_records,
                "events": events,
            }
        )
        evidence["final_tick"] = owner["tick"]
        _ = served_owner
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
        choices=["expect-standing"],
        help="doctor the post-re-adoption expectation to the "
        "substitution still standing — the pass must fail naming the "
        "evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = stale_checkpoint_pass(
            args, args.tamper
        )
    except Abort as abort:
        for line in abort.args:
            eprint(f"stale-checkpoint: {line}")
        return 1
    for failure in failures:
        eprint(f"stale-checkpoint: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"stale-checkpoint: the {args.tamper} case passed "
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
        f"stale-checkpoint-digest {digest} — tracking by tick "
        f"{evidence['converged']}, forced at tick "
        f"{evidence['forced_at']}, switched at tick "
        f"{evidence['switched_at']}, released at tick "
        f"{evidence['released_at']}, resumed at tick "
        f"{evidence['resumed_tick']}, tracking again by tick "
        f"{evidence['reconverged']}, roles restored at tick "
        f"{evidence['roles_restored_at']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
