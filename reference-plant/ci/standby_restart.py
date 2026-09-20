#!/usr/bin/env python3
"""The standby-restart leg for the reference plant — the pair half of
WW-LCM-001's restart-recovery evidence on the consumer-declared
deployment (WW-ENG-003): the tracking peer of the manifest-declared
pair stopped and relaunched onto its declared
`--state-file`/`--journal-file`, asserting it resumes, rejoins in
standby, and reconverges to tracking while the field owner's
ownership never falters.

The restart-recovery leg (`ci/restart.py`) proves the lone-controller
clause — a released controller resuming from its declared
`--state-file` in the run's tick domain with the durable journal
carrying the boundary — and the pair leg (`ci/pair.py`) proves
tracking, the receipted switch, and the persisted record on the
running pair. This leg exercises the standby half of restart
recovery on the deployed pair: the non-field-owning peer restarted
onto its declared state file must resume, re-pull, and reconverge to
tracking while the active keeps writing the field. The run:

- converges the manifest-declared pair to `tracking` through the
  pair rig's driven-tick loop and settles a kind-declared command
  `applied` into both peers' adopted receipt log — the pre-restart
  run leaving journaled entries the restart boundary must order
  after;
- stops the tracking standby's container and keeps driving the field
  owner through the downtime window: each driven scan's writes
  landing on the simulated field, a second receipted command
  settling `applied`, and the active's role and journal undisturbed;
- relaunches the standby on the manifest's wiring with its declared
  persistence files: the startup preamble must report the resume at
  the persisted tick — never a silent cold start at tick zero — the
  served role surface reports `standby` unsynchronized rather than a
  field claim, the replayed journal answers the pre-restart entries
  verbatim behind the run-2 boundary entry, and the durable file
  carries that boundary ordered after run 1's entries with the `seq`
  order continuing across it;
- drives the pair's tracking-first ticks until the resumed peer
  reports `tracking` inside the leg's declared reconvergence window
  — the field owner's writes landing throughout — then audits the
  active's journal for an untouched, append-only record and the
  peers' receipt logs for one identical adopted log carrying the
  downtime command;
- issues the documented `demote`/`promote` switch on the reconverged
  pair: the restarted peer promoting and the demoted owner
  reconverging to `tracking` on it — proving the restart left no
  wedge for later legs.

Usage:

    standby_restart.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `standby-restart-digest <sha256>` line prints — the
check runs two passes and compares them
(`standby-restart-nondeterministic`). A contract violation reports
`standby-restart: …` lines on stderr and exits 1 — the check's
`standby-restart-failed`. `--tamper missing-state-file` removes the
standby's persisted checkpoint at the restart point — the relaunch
cold-starts and the leg's resume assertions name the loss;
`--tamper skip-restart` never restarts the peer but keeps the
restart assertions — each doctored pass must exit nonzero carrying
its evidence.
"""

import argparse
import hashlib
import json
import os
import re
import sys

import pair
import refusal
import simulate


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The driven ticks each window runs — the downtime window the field
# owner keeps writing through while its standby is dead, and the
# leg's declared reconvergence window the resumed peer must report
# tracking inside. The actor the leg's receipted submissions declare.
DOWNTIME_TICKS = 3
RECONVERGE_TICKS = 4
ACTOR = "ci-standby-restart"


def declared_command(url, failures):
    """One `(component, spec, command)` the peer's served registry
    declares natively — the `invoke` the leg submits."""
    schema = pair.get(f"{url}/schema", "GET /schema", failures)
    declared = pair.declared_command(schema)
    if declared is None:
        failures.append(
            "the served registry declares no command — the leg's "
            "receipted path has nothing to exercise"
        )
        raise Abort
    component, spec = declared
    command = {
        "invoke": {
            "component": component,
            "command": spec["name"],
            "arguments": simulate.command_arguments(spec),
        }
    }
    return component, spec, command


def submit(url, command, failures):
    """`POST /command` asserting an accepted receipt; returns it."""
    status, receipt = pair.request(
        f"{url}/command", {"command": command, "actor": ACTOR}
    )
    if status != 200 or simulate.receipt_outcome(receipt) != "accepted":
        failures.append(
            f"the declared command {command['invoke']['command']} on "
            f"{command['invoke']['component']} answered {status} "
            f"{receipt}, expected an accepted receipt"
        )
        raise Abort
    return receipt


def applied(receipts, command):
    """The applied receipts one submitted command settled into a
    served receipt log."""
    return [
        entry
        for entry in receipts
        if entry.get("command") == command
        and simulate.receipt_outcome(entry) == "applied"
    ]


def role(url, failures):
    """The peer's served RoleReport."""
    return pair.get(f"{url}/role", "GET /role", failures)


def tracking(report):
    """Whether a RoleReport reads `standby` under `tracking` sync."""
    sync = report.get("sync")
    return report.get("role") == "standby" and (
        isinstance(sync, dict) and "tracking" in sync
    )


def field_check(owner, plant_io, failures, when):
    """The field-mismatch audit one driven owner scan leaves — every
    simulated `out` point holding the owner's served image. Returns
    the field samples for the digest."""
    field = refusal.field_out_samples(plant_io)
    for mismatch in refusal.field_mismatches(owner, field):
        failures.append(f"{mismatch} {when} — the active's field writes faltered")
    if failures:
        raise Abort
    return field


def standby_restart_pass(args, tamper):
    """The standby-restart run: converge, command, restart the
    tracking peer onto its declared files, reconverge, switch.
    Returns `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "standby-restart leg has nothing to exercise"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        standby_files = rig.standby_files
        state_file = standby_files.get("state_file")
        journal_file = standby_files.get("journal_file")
        if state_file is None or journal_file is None:
            raise Abort(
                "the manifest's standby declares no "
                "state_file/journal_file — the standby-restart leg "
                "has nothing to exercise"
            )

        # Phase 1 — convergence, then a kind-declared command
        # receipted `accepted` on the owner settling `applied` into
        # both peers' adopted log: the pre-restart run leaves
        # journaled entries the restart boundary must order after.
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
        component, spec, command = declared_command(duty_url, failures)
        receipt = submit(duty_url, command, failures)
        # The settling tick carries the same one-cycle adopted-receipt
        # lag the pair leg absorbs; bounded extra cycles heal a
        # field-moving invoke's verdict before the restart point.
        _tracked, owner = pair.tick(standby_url, duty_url, failures)
        for _ in range(5):
            if tracking(role(standby_url, failures)):
                break
            pair.scan(duty_url, failures)
            _tracked, owner = pair.tick(standby_url, duty_url, failures)
        else:
            failures.append(
                "the tracking peer never reconverged after the "
                "command's settling tick"
            )
            raise Abort
        receipts_duty = pair.get(
            f"{duty_url}/receipts", "GET /receipts", failures
        )
        receipts_standby = pair.get(
            f"{standby_url}/receipts", "GET /receipts", failures
        )
        if receipts_duty != receipts_standby:
            failures.append(
                "the peers' receipt logs diverged — the adopted audit "
                "is not one log"
            )
            raise Abort
        if not applied(receipts_duty, command):
            failures.append(
                f"the declared command {spec['name']} on {component} "
                "never settled applied into the adopted receipt log"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "command",
                "component": component,
                "command": spec["name"],
                "receipt": receipt,
                "receipts": receipts_duty,
            }
        )

        # Phase 2 — the restart point: the tracking peer's last served
        # tick and both peers' served journals captured, then the
        # standby's container stops while the field owner keeps
        # running. The declared files must hold the tracking run's
        # checkpoint and run-1 record — the journaled entries the
        # restart boundary must order after.
        stopped = pair.get(
            f"{standby_url}/snapshot", "GET /snapshot", failures
        )
        served_standby = pair.get(
            f"{standby_url}/journal", "GET /journal", failures
        )
        served_duty = pair.get(
            f"{duty_url}/journal", "GET /journal", failures
        )
        if tamper != "skip-restart":
            pair.stop(rig.standby)
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
                f"the standby's persisted state file does not parse: {error}"
            )
            raise Abort
        persisted = checkpoint.get("tick")
        evidence["persisted_tick"] = persisted
        if persisted != stopped["tick"]:
            failures.append(
                f"the standby's state file persisted tick {persisted} "
                f"while the tracking run stood at {stopped['tick']}"
            )
            raise Abort
        if checkpoint.get("model_fingerprint") != rig.fingerprint:
            failures.append(
                f"the standby's state file carries fingerprint "
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
                "the standby's run-1 journal carries no settled "
                "receipt — the restart point precedes the entries the "
                "boundary must order after"
            )
            raise Abort
        evidence["run1_entries"] = len(run1_entries)

        # Phase 3 — the downtime window: the standby dead, the field
        # owner's driven scans keep writing the field — every `out`
        # point holding the owner's served image — and a second
        # declared command settles `applied` through the receipted
        # path. The active's field ownership never falters.
        downtime = []
        _component, _spec, down_command = declared_command(
            duty_url, failures
        )
        down_receipt = None
        for index in range(DOWNTIME_TICKS):
            owner = pair.scan(duty_url, failures)
            field = field_check(
                owner,
                rig.plant_io,
                failures,
                "while the standby was down",
            )
            downtime.append({"tick": owner["tick"], "field": field})
            if index == 0:
                down_receipt = submit(duty_url, down_command, failures)
        receipts_duty = pair.get(
            f"{duty_url}/receipts", "GET /receipts", failures
        )
        if not applied(receipts_duty, down_command):
            failures.append(
                f"the declared command {down_command['invoke']['command']} "
                f"on {down_command['invoke']['component']} never settled "
                "applied on the field owner during the standby's downtime"
            )
            raise Abort
        duty_role = role(duty_url, failures)
        if duty_role.get("role") != "active":
            failures.append(
                f"the field owner reports {duty_role.get('role')!r} "
                "after the downtime window, expected active — the "
                "standby's death disturbed it"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "downtime",
                "scans": downtime,
                "command": down_command["invoke"]["command"],
                "receipt": down_receipt,
            }
        )

        # Phase 4 — the relaunch onto the declared files: the preamble
        # must report the resume at the persisted tick, the served
        # snapshot must stand at it, and the served role surface must
        # read `standby` unsynchronized — the resumed peer rejoins in
        # standby, never claiming the field. The replayed journal
        # answers the pre-restart entries verbatim behind the run-2
        # boundary entry, and the durable file carries that boundary
        # ordered after run 1's entries with `seq` order intact.
        if tamper == "missing-state-file":
            os.remove(state_file)
        preamble = []
        if tamper != "skip-restart":
            rig.standby, standby_url, preamble = pair.spawn_peer(
                args.controller,
                args.model,
                args.dt,
                rig.plant_addr,
                rig.duty_url.removeprefix("http://"),
                standby_files,
            )
            rig.standby_url = standby_url
            if standby_url is None:
                detail = "; ".join(preamble[-2:]) or "no diagnostic"
                failures.append(
                    f"the relaunched standby exited at startup: {detail}"
                )
                raise Abort
        line = next(
            (line for line in preamble if "resumed from state file" in line),
            None,
        )
        if line is None:
            failures.append(
                "the relaunched standby never reported a resume — its "
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
            f"{standby_url}/snapshot", "GET /snapshot", failures
        )
        if snapshot["tick"] != persisted:
            failures.append(
                f"the resumed standby reports tick {snapshot['tick']}, "
                f"the persisted tick is {persisted}"
            )
            raise Abort
        standby_role = role(standby_url, failures)
        if standby_role.get("role") != "standby" or (
            standby_role.get("sync") != "unsynchronized"
        ):
            failures.append(
                f"the resumed standby reports {standby_role} — "
                "expected standby/unsynchronized: it must rejoin in "
                "standby, never claiming the field"
            )
            raise Abort
        duty_role = role(duty_url, failures)
        if duty_role.get("role") != "active":
            failures.append(
                f"the field owner reports {duty_role.get('role')!r} "
                "after the standby's relaunch, expected active — the "
                "resumed peer claimed the field"
            )
            raise Abort
        replayed = pair.get(
            f"{standby_url}/journal", "GET /journal", failures
        )
        if replayed[: len(served_standby)] != served_standby:
            failures.append(
                "the standby's replayed journal no longer answers the "
                "pre-restart entries verbatim"
            )
            raise Abort
        boundary = {
            "seq": len(served_standby) + 1,
            "tick": persisted,
            "event": {"run_boundary": {"run": 2}},
        }
        if replayed[len(served_standby) :] != [boundary]:
            failures.append(
                "the resumed standby's served journal does not open "
                f"with the run-2 boundary entry {boundary}: "
                f"{replayed[len(served_standby):]}"
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
                f"the standby's journal boundaries are {boundaries}, "
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
                "the standby's restart boundary is not ordered after "
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
                f"the standby's journal seqs do not continue 1..n "
                f"across the restart boundary: {seqs}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "restart",
                "persisted_tick": persisted,
                "resumed_tick": resumed,
                "standby_role": standby_role,
                "boundaries": boundaries,
            }
        )

        # Phase 5 — reconvergence: driven tracking-first ticks until
        # the resumed peer reports `tracking` inside the leg's
        # declared window — the field owner's writes landing
        # throughout, the active's role never moving.
        reconverged = None
        ticks = []
        for _ in range(RECONVERGE_TICKS):
            _tracked, owner = pair.tick(
                standby_url,
                duty_url,
                failures,
                diverged="the resumed standby's image diverged from "
                "the field owner's at tick {tick} — the restart's "
                "re-pull never realigned the pair",
            )
            ticks.append(owner["tick"])
            field_check(
                owner,
                rig.plant_io,
                failures,
                "after the standby's restart",
            )
            standby_role = role(standby_url, failures)
            if tracking(standby_role):
                reconverged = standby_role
                break
        if reconverged is None:
            failures.append(
                "the resumed standby never reconverged to tracking "
                f"inside the declared {RECONVERGE_TICKS}-tick window — "
                f"GET /role answers {standby_role}"
            )
            raise Abort
        evidence["reconverged"] = ticks[-1]
        digest_entries.append(
            {
                "phase": "reconverged",
                "ticks": ticks,
                "standby_role": reconverged,
            }
        )

        # Phase 6 — the undisturbed audit: the active's served journal
        # answers its pre-restart entries verbatim with only the
        # downtime record appended — no restart boundary, no role
        # record — and its durable file carries the same untouched
        # record; the peers' adopted receipt logs read as one, the
        # downtime command included.
        duty_journal = pair.get(
            f"{duty_url}/journal", "GET /journal", failures
        )
        if duty_journal[: len(served_duty)] != served_duty:
            failures.append(
                "the active's served journal rewrote its pre-restart "
                "entries across the standby's restart"
            )
            raise Abort
        disturbed = [
            entry
            for entry in duty_journal
            if "role_changed" in entry.get("event", {})
            or "run_boundary" in entry.get("event", {})
        ]
        if disturbed:
            failures.append(
                "the active's journal carries a disturbance across "
                f"the standby's restart: {disturbed}"
            )
            raise Abort
        receipts_standby = pair.get(
            f"{standby_url}/receipts", "GET /receipts", failures
        )
        receipts_duty = pair.get(
            f"{duty_url}/receipts", "GET /receipts", failures
        )
        if receipts_duty != receipts_standby:
            failures.append(
                "the resumed standby's adopted receipt log diverged "
                "from the field owner's — the re-pull did not restore "
                "one log"
            )
            raise Abort
        if not applied(receipts_standby, down_command):
            failures.append(
                "the downtime command never reached the resumed "
                "standby's adopted receipt log"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "undisturbed",
                "duty_journal": duty_journal,
                "receipts": receipts_duty,
            }
        )

        # Phase 7 — the pair still promotes: the documented
        # demote/promote switch on the reconverged pair — the
        # restarted peer settling `active`, the demoted owner
        # reconverging `tracking` on it — proving the restart left no
        # wedge for later legs.
        switched = rig.switch(
            duty_url, standby_url, failures, audit_receipts=True
        )
        evidence["promoted"] = switched["promote"]["tick"]
        digest_entries.append(
            {
                "phase": "switch",
                "demote": switched["demote"],
                "promote": switched["promote"],
                "ticks": switched["ticks"],
                "demoted_role": switched["demoted_role"],
                "promoted_role": switched["promoted_role"],
                "receipts": switched["receipts"],
            }
        )
        evidence["final_tick"] = switched["owner"]["tick"]
        records = pair.journal_records(journal_file)
        evidence["journal_records"] = len(records)
        digest_entries.append(
            {"phase": "record", "standby_journal_records": records}
        )
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
        choices=["missing-state-file", "skip-restart"],
        help="doctor the restart point — the pass must fail naming "
        "the evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = standby_restart_pass(
            args, args.tamper
        )
    except Abort as abort:
        for line in abort.args:
            eprint(f"standby-restart: {line}")
        return 1
    for failure in failures:
        eprint(f"standby-restart: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"standby-restart: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored restart"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"standby-restart-digest {digest} — tracking by tick "
        f"{evidence['converged']}, resumed at tick "
        f"{evidence['resumed_tick']}, tracking again by tick "
        f"{evidence['reconverged']}, promoted at tick "
        f"{evidence['promoted']}, run continued to tick "
        f"{evidence['final_tick']}, {evidence['journal_records']} "
        "persisted journal records"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
