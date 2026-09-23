#!/usr/bin/env python3
"""The journal run-boundary flood leg for the reference plant — the
consumer-side proof that the deployed redundant pair's lifetime
attribution survives ordinary event volume: the rig-side
journal-flood finding's (#623, landed fix) mirror on the
customer-owned pair (WW-ENG-003, WW-LCM-001).

The restart legs (`ci/restart.py`, `ci/standby_restart.py`) prove a
run-boundary marker lands in the durable journal across a restart;
this leg proves the marker still reaches a `GET /journal` consumer
after the served tail's bound evicts everything around it. The rig
reads the standby wiring and persistence fields out of
`deploy/manifest.json` and launches the released tooling exactly as
the pair legs do — no new wire types, no journal-format change. The
run:

- converges the manifest-declared pair to `tracking` through the
  pair rig's driven-tick loop;
- restarts the tracking standby onto its declared
  `--state-file`/`--journal-file`, so its durable record gains the
  run-2 boundary marker and its served journal the boundary entry a
  lifetime-attributing consumer reads;
- floods the field owner's receipted path with more journal-producing
  commands than the served journal's retained bound — each settled
  `command_settled` journaled on the owner and adopted into the
  tracking peer's journal a pull later — so the run-2 boundary entry
  ages out of the retained tail on both peers;
- restarts the standby a second time: the replay finds run 2's
  boundary entry already outside the file's retained tail and must
  pin it aside rather than drop it — the recovery half of the
  rig-side finding — then floods past the bound again so run 3's
  boundary entry must survive the same way;
- audits the record: the standby's served `GET /journal` must still
  answer both lifetimes' `run_boundary` entries ahead of the retained
  tail — strictly `seq`-ordered with the ordinary eviction between
  the last boundary and the tail reading as the usual numbering gap,
  and the `?since=` cursor past the last boundary answering exactly
  the retained tail — while the durable file retains every
  `run_boundary` marker in order with contiguous `seq`s; the field
  owner's single-lifetime journal stays bounded the same way, its
  file retaining the cold-start marker; and the pair's roles never
  move.

Usage:

    journal_boundary.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `journal-boundary-digest <sha256>` line prints — the
check runs two passes and compares them
(`journal-boundary-nondeterministic`). A contract violation reports
`journal-boundary-failed: …` lines on stderr and exits 1 — the
check's `journal-boundary-failed`. `--tamper dropped-boundaries`
doctors the leg's own read of the served journal so its tail loses
every `run_boundary` entry while the durable file retains them — the
boundary audit must report the named diagnostic rather than pass
silently.
"""

import argparse
import hashlib
import json
import re
import sys

import pair
import simulate


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The flood: submissions per driven owner scan — inside the released
# controller's 64-command admission bound — and the per-phase count:
# past the served journal's 1024-entry retained bound so the
# run-boundary entries the flood follows must age out of the tail.
FLOOD_BATCH = 60
FLOOD_COMMANDS = 1140
JOURNAL_BOUND = 1024

# The driven pair ticks a relaunched standby gets to re-track before
# the next phase — the pulls that rejoin it to the owner's checkpoint
# stream are the same scans that adopt the flood's receipts.
RECONVERGE_TICKS = 4

# The served boundary runs the audit expects on the twice-restarted
# standby, and the marker runs its durable file must retain — run 1's
# cold-start marker is the file's own record, never a served entry.
SERVED_RUNS = [2, 3]
FILE_RUNS = [1, 2, 3]

ACTOR = "ci-journal-boundary"


def writable_bool_point(model):
    """The lowest-id writable boolean `in` point the emitted model
    declares — the receipted `write_value` target the flood drives,
    the same honest seam the role-gated write leg uses."""
    writable = [
        point["id"]
        for point in sorted(model["io_points"], key=lambda entry: entry["id"])
        if point.get("writable")
        and point["direction"] == "in"
        and point["value_type"] == "bool"
    ]
    return writable[0] if writable else None


def boundary_entries(journal):
    """A served journal's `run_boundary` entries in served order —
    the pinned stream a lifetime-attributing consumer reads."""
    return [
        entry
        for entry in journal
        if "run_boundary" in entry.get("event", {})
    ]


def served_journal(url, failures, tamper):
    """`GET /journal` with the leg's own tamper applied: under
    `dropped-boundaries` the read drops every `run_boundary` entry —
    the doctored served tail the boundary audit must refuse while the
    durable file still retains its markers."""
    journal = pair.get(f"{url}/journal", "GET /journal", failures)
    if tamper == "dropped-boundaries":
        journal = [
            entry
            for entry in journal
            if "run_boundary" not in entry.get("event", {})
        ]
    return journal


def flood(rig, write, failures):
    """Drive FLOOD_COMMANDS journal-producing commands through the
    field owner's receipted path, FLOOD_BATCH per driven scan — every
    submission must answer `accepted`, and each settles into a
    `command_settled` entry on the owner's next scan boundary and
    into the tracking peer's adopted journal on its following pull.
    Returns the submitted count."""
    submitted = 0
    while submitted < FLOOD_COMMANDS:
        for _ in range(min(FLOOD_BATCH, FLOOD_COMMANDS - submitted)):
            status, receipt = pair.request(
                f"{rig.duty_url}/command",
                {"command": write, "actor": ACTOR},
            )
            if status != 200 or simulate.receipt_outcome(receipt) != "accepted":
                failures.append(
                    f"journal-boundary-failed: the flood's write_value "
                    f"answered {status} {receipt}, expected an "
                    "accepted receipt — the receipted path refused "
                    "ordinary event volume"
                )
                raise Abort
            submitted += 1
        pair.scan(rig.duty_url, failures)
        pair.scan(rig.standby_url, failures)
    return submitted


def restart_standby(rig, args, failures, run):
    """Stop the tracking peer and relaunch it onto its declared
    persistence files — the manifest's own wiring — so the durable
    journal gains run `run`'s boundary marker and the served journal
    its boundary entry. Asserts the declared files persisted the
    stopping tick and the relaunch resumed at it. Returns the resumed
    tick."""
    stopped = pair.get(
        f"{rig.standby_url}/snapshot", "GET /snapshot", failures
    )
    pair.stop(rig.standby)
    state_file = rig.standby_files.get("state_file")
    try:
        with open(state_file) as handle:
            persisted = json.load(handle).get("tick")
    except (OSError, json.JSONDecodeError, TypeError) as error:
        failures.append(
            f"journal-boundary-failed: the standby's declared state "
            f"file does not parse before run {run}'s restart: {error}"
        )
        raise Abort
    if persisted != stopped["tick"]:
        failures.append(
            f"journal-boundary-failed: the standby's state file "
            f"persisted tick {persisted} while the tracking run "
            f"stood at {stopped['tick']} before run {run}'s restart"
        )
        raise Abort
    rig.standby, url, preamble = pair.spawn_peer(
        args.controller,
        args.model,
        args.dt,
        rig.plant_addr,
        rig.duty_url.removeprefix("http://"),
        rig.standby_files,
        pair_token=pair.PAIR_TOKEN,
    )
    rig.standby_url = url
    if url is None:
        failures.append(
            f"journal-boundary-failed: the standby relaunched for "
            f"run {run} exited at startup: "
            f"{'; '.join(preamble[-2:]) or 'no diagnostic'}"
        )
        raise Abort
    line = next(
        (line for line in preamble if "resumed from state file" in line),
        None,
    )
    if line is None:
        failures.append(
            f"journal-boundary-failed: the standby relaunched for "
            f"run {run} never reported a resume — a cold start at "
            f"tick 0 silently abandons the persisted run at tick "
            f"{persisted}"
        )
        raise Abort
    match = re.search(r"at tick (\d+)", line)
    resumed = int(match.group(1)) if match else None
    if resumed != persisted:
        failures.append(
            f"journal-boundary-failed: the standby relaunched for "
            f"run {run} resumed at tick {resumed}, the state file "
            f"persisted {persisted}"
        )
        raise Abort
    # The reconvergence ticks: the resumed peer's driven scans pull
    # and apply the owner's checkpoints until it re-tracks — the same
    # per-scan pull the flood's adoptions ride.
    for _ in range(RECONVERGE_TICKS):
        pair.scan(rig.standby_url, failures)
        pair.scan(rig.duty_url, failures)
    report = pair.get(f"{rig.standby_url}/role", "GET /role", failures)
    sync = report.get("sync")
    if report.get("role") != "standby" or not (
        isinstance(sync, dict) and "tracking" in sync
    ):
        failures.append(
            f"journal-boundary-failed: the standby relaunched for "
            f"run {run} never re-tracked — GET /role answers "
            f"{report}"
        )
        raise Abort
    return resumed


def audit_peer(name, journal, served_runs, markers, failures):
    """The boundary audit one peer's record must pass: the served
    journal still exposes every served lifetime's `run_boundary`
    entry — pinned ahead of the retained tail with the flood's
    ordinary eviction reading as the `seq` gap between the last
    boundary and the tail's first entry — the durable file retains
    every `run_boundary` marker in order, and the entry `seq`s stay
    contiguous across the file's whole record. Returns the peer's
    audit digest entry."""
    boundaries = boundary_entries(journal)
    runs = [entry["event"]["run_boundary"]["run"] for entry in boundaries]
    if runs != served_runs:
        failures.append(
            f"journal-boundary-failed: {name}'s served journal "
            f"carries run boundaries {runs} after the flood, "
            f"expected {served_runs} — lifetime attribution was "
            "lost under ordinary event volume while the durable "
            f"file retains markers {markers}"
        )
        raise Abort
    if journal[: len(boundaries)] != boundaries:
        failures.append(
            f"journal-boundary-failed: {name}'s served journal does "
            "not pin its run-boundary entries ahead of the retained "
            f"tail: {journal[: len(boundaries) + 2]}"
        )
        raise Abort
    seqs = [entry["seq"] for entry in journal]
    if seqs != sorted(seqs) or len(set(seqs)) != len(seqs):
        failures.append(
            f"journal-boundary-failed: {name}'s served journal is "
            f"not in strict seq order after the flood: {seqs}"
        )
        raise Abort
    tail = journal[len(boundaries) :]
    if boundaries and tail:
        if len(tail) > JOURNAL_BOUND:
            failures.append(
                f"journal-boundary-failed: {name}'s served journal "
                f"retains {len(tail)} tail entries, over the "
                f"{JOURNAL_BOUND}-entry bound"
            )
            raise Abort
        if tail[0]["seq"] <= boundaries[-1]["seq"] + 1:
            failures.append(
                f"journal-boundary-failed: {name}'s served journal "
                "shows no seq gap between its last run-boundary "
                f"entry (seq {boundaries[-1]['seq']}) and the "
                f"retained tail (seq {tail[0]['seq']}) — the "
                "flood's ordinary eviction did not happen"
            )
            raise Abort
    return {
        "served_boundaries": [
            {
                "seq": entry["seq"],
                "tick": entry["tick"],
                "run": entry["event"]["run_boundary"]["run"],
            }
            for entry in boundaries
        ],
        "tail": len(tail),
    }


def boundary_pass(args, tamper):
    """The journal-boundary run: converge, restart, flood, restart,
    flood, audit. Returns `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "journal-boundary leg has nothing to exercise"
        )
    _manifest, _duty_decl, _standby_decl = declared
    with open(args.model) as handle:
        model = json.load(handle)
    point = writable_bool_point(model)
    if point is None:
        raise Abort(
            "the emitted model declares no writable boolean point — "
            "the journal-boundary leg has nothing to flood"
        )
    write = {
        "write_value": {
            "kind": "bool",
            "point": point,
            "value": {"bool": True},
        }
    }
    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url = rig.duty_url
        standby_files = rig.standby_files
        if (
            standby_files.get("state_file") is None
            or standby_files.get("journal_file") is None
            or rig.duty_files.get("journal_file") is None
        ):
            raise Abort(
                "the manifest's pair declares no "
                "state_file/journal_file persistence — the "
                "journal-boundary leg has nothing to exercise"
            )

        # Phase 1 — convergence: the pair leg's driven-tick loop, the
        # tracking peer scanned first so each pull applies the owner's
        # latest checkpoint and the peers rest at the same tick.
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

        # Phase 2 — the first restart: the tracking standby stopped
        # and relaunched onto its declared files, so run 2 begins —
        # the durable record gaining the run-2 marker, the served
        # journal the boundary entry.
        resumed2 = restart_standby(rig, args, failures, 2)
        evidence["run2_tick"] = resumed2
        digest_entries.append({"phase": "restart", "run": 2, "tick": resumed2})

        # Phase 3 — the first flood: more journal-producing commands
        # than the served journal's retained bound, each receipted
        # `accepted` on the field owner, settling `command_settled`
        # at its scan boundary, and adopted into the tracking peer's
        # journal on its pull — so run 2's boundary entry ages out of
        # both peers' retained tails.
        flooded = flood(rig, write, failures)
        evidence["flood_a"] = flooded
        digest_entries.append(
            {"phase": "flood", "commands": flooded, "after_run": 2}
        )

        # Phase 4 — the second restart: run 3 begins. The replay must
        # pin run 2's boundary entry aside — the file's retained tail
        # already starts after it — and journal run 3's own.
        resumed3 = restart_standby(rig, args, failures, 3)
        evidence["run3_tick"] = resumed3
        digest_entries.append({"phase": "restart", "run": 3, "tick": resumed3})

        # Phase 5 — the second flood: past the retained bound again,
        # so run 3's boundary entry ages out of the served tail the
        # same way.
        flooded = flood(rig, write, failures)
        evidence["flood_b"] = flooded
        digest_entries.append(
            {"phase": "flood", "commands": flooded, "after_run": 3}
        )

        # Phase 6 — the audit. The standby's served journal must
        # still answer both lifetimes' boundary entries ahead of the
        # retained tail with the flood's eviction reading as the seq
        # gap, the `?since=` cursor past the last boundary answering
        # exactly that tail, and the durable file retaining every
        # marker in order with contiguous seqs. The field owner's
        # single-lifetime journal carries no served boundary by
        # contract — the cold-start marker is the file's own record —
        # but the same bounded-tail and file-retention rules audit.
        served_standby = served_journal(rig.standby_url, failures, tamper)
        served_duty = served_journal(duty_url, failures, tamper)
        audit = audit_peer(
            "the standby",
            served_standby,
            SERVED_RUNS,
            FILE_RUNS,
            failures,
        )
        if audit["served_boundaries"][0]["tick"] != resumed2 or (
            audit["served_boundaries"][1]["tick"] != resumed3
        ):
            failures.append(
                "journal-boundary-failed: the standby's served "
                f"boundary ticks are "
                f"{[entry['tick'] for entry in audit['served_boundaries']]}, "
                f"expected the resume ticks {resumed2} and {resumed3}"
            )
            raise Abort
        tail = served_standby[len(SERVED_RUNS) :]
        since = pair.get(
            f"{rig.standby_url}/journal?since={audit['served_boundaries'][-1]['seq']}",
            "GET /journal?since",
            failures,
        )
        if since != tail:
            failures.append(
                "journal-boundary-failed: the standby's "
                "?since=<last boundary> read did not answer exactly "
                "the retained tail — the seq-cursor semantics broke "
                "under the flood"
            )
            raise Abort
        records = pair.journal_records(rig.standby_files["journal_file"])
        markers = [record for kind, record in records if kind == "boundary"]
        expected_markers = [
            {"run": 1, "tick": 0},
            {"run": 2, "tick": resumed2},
            {"run": 3, "tick": resumed3},
        ]
        if markers != expected_markers:
            failures.append(
                "journal-boundary-failed: the standby's durable "
                f"journal retains markers {markers}, expected "
                f"{expected_markers} — the file lost a lifetime "
                "boundary under the flood"
            )
            raise Abort
        entries = [record for kind, record in records if kind == "entry"]
        seqs = [entry["seq"] for entry in entries]
        if seqs != list(range(1, len(seqs) + 1)):
            failures.append(
                "journal-boundary-failed: the standby's durable "
                f"journal seqs are not 1..n contiguous across the "
                f"run boundaries: {seqs[:8]}…{seqs[-4:]}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "audit",
                "peer": "standby",
                **audit,
                "file_markers": markers,
                "file_entries": len(entries),
            }
        )

        duty_audit = audit_peer(
            "the field owner",
            served_duty,
            [],
            [{"run": 1, "tick": 0}],
            failures,
        )
        records = pair.journal_records(rig.duty_files["journal_file"])
        markers = [record for kind, record in records if kind == "boundary"]
        if markers != [{"run": 1, "tick": 0}]:
            failures.append(
                "journal-boundary-failed: the field owner's durable "
                f"journal retains markers {markers}, expected the "
                "single cold-start marker — the file gained or lost "
                "a lifetime under the flood"
            )
            raise Abort
        entries = [record for kind, record in records if kind == "entry"]
        seqs = [entry["seq"] for entry in entries]
        if seqs != list(range(1, len(seqs) + 1)):
            failures.append(
                "journal-boundary-failed: the field owner's durable "
                "journal seqs are not 1..n contiguous under the "
                "flood"
            )
            raise Abort
        if served_duty and served_duty[0]["seq"] <= 1:
            failures.append(
                "journal-boundary-failed: the field owner's served "
                "journal still starts at seq 1 after the flood — "
                "the retained tail never aged, so the bound is not "
                "exercised"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "audit",
                "peer": "duty",
                **duty_audit,
                "file_markers": markers,
                "file_entries": len(entries),
            }
        )

        # The pair never moved: the flood and both restarts left the
        # field owner active and the tracking peer in standby.
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        standby_role = pair.get(f"{rig.standby_url}/role", "GET /role", failures)
        sync = standby_role.get("sync")
        if duty_role.get("role") != "active" or (
            standby_role.get("role") != "standby"
            or not (isinstance(sync, dict) and "tracking" in sync)
        ):
            failures.append(
                "journal-boundary-failed: the pair's roles moved "
                f"under the flood — the field owner reports "
                f"{duty_role}, the tracking peer {standby_role}"
            )
            raise Abort
        evidence["final_tick"] = pair.get(
            f"{duty_url}/snapshot", "GET /snapshot", failures
        )["tick"]
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
        choices=["dropped-boundaries"],
        help="doctor the leg's own served-journal read so its tail "
        "loses every run_boundary while the durable file retains "
        "them — the pass must fail naming the boundary evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = boundary_pass(args, args.tamper)
    except Abort as abort:
        for line in abort.args:
            eprint(f"journal-boundary: {line}")
        return 1
    for failure in failures:
        if failure.startswith("journal-boundary-failed:"):
            eprint(failure)
        else:
            eprint(f"journal-boundary: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"journal-boundary: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored "
                "served tail"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"journal-boundary-digest {digest} — {evidence['flood_a']}+"
        f"{evidence['flood_b']} receipted commands flooded past the "
        f"{JOURNAL_BOUND}-entry bound, the standby's served journal "
        "still answering run boundaries [2, 3] ahead of the retained "
        "tail, its file retaining markers [1, 2, 3], tracking at "
        f"tick {evidence['final_tick']} (converged at "
        f"{evidence['converged']})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
