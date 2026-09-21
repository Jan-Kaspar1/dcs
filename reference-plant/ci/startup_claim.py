#!/usr/bin/env python3
"""The startup-claim ordering leg for the reference plant — the
consumer-side proof that a released `dcs-controller` validates its
local startup inputs before taking the plant's preemptive write
claim, so a doomed startup can never strand a persistent claim
fencing the healthy incumbent (WW-ENG-003, WW-LCM-001).

The pair leg (`ci/pair.py`) proves the declared pair converges and
switches, and the negotiation leg (`ci/negotiation.py`) proves a
misconfigured third peer degrades honestly. This leg proves the
remaining third-controller case — the misdeployment a customer's own
operations produce: a released controller launched against the same
plant address whose startup inputs are doomed by construction, its
`--journal-file` carrying a first record the startup replay cannot
read. A launched active's preemptive write-ownership claim is the
run's first irreversible shared-field side effect, so the released
binary takes it only after every fallible local startup step — the
journal replay and the monitor bind — has succeeded; a starter that
fails earlier must leave the incumbent's claim, roles, and field
writes untouched. The run:

- converges the manifest-declared pair to `tracking` and issues the
  documented demote/promote switch, so the field owner holds the
  plant's writer claim: a foreign attachment's mutation probe answers
  `fenced` and the incumbent's writes land;
- launches the doomed peer — `dcs-controller <model> --remote …
  --driven` with the corrupt `--journal-file` and a declared
  `--state-file` path under the rig's scratch: the spawn must exit at
  startup naming the replay failure, never reporting a listener and
  never logging the claim line;
- drives the observation window through `POST /scan` on the settled
  pair: every round the incumbent stays `active`, the tracking peer
  stays `tracking`, the peers' images stay identical, the incumbent's
  tick advances, its field writes keep landing, and the foreign
  probe stays `fenced` under the standing claim — while one
  kind-declared command submitted mid-window to the incumbent is
  receipted `accepted` and settles `applied` on both peers;
- audits the post-attempt positions: the incumbent's served journal
  gained no role, fencing, divergence, or restart records, the doomed
  peer's journal file still holds exactly the corrupt record, and its
  state file never appeared;
- stops the foreign process and restores the pair's launch roles —
  the pair returns clean, later legs see the same rig.

Usage:

    startup_claim.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `startup-claim-digest <sha256>` line prints — the
check runs two passes and compares them
(`startup-claim-nondeterministic`). A contract violation reports
`startup-claim: …` lines on stderr and exits 1 — the check's
`startup-claim-failed`. `--tamper stranded-claim` lands the pre-fix
defect's observable shape by hand — a foreign attachment takes the
plant's writer claim and drops its hold unreleased, stranding the
dead claim over the incumbent — so the leg proves its disturbance
assertions fire rather than passing an unexercised contract.
"""

import argparse
import hashlib
import json
import os
import sys

import failover
import negotiation
import pair
import simulate


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The driven ticks the observation window runs once the doomed peer
# is launched — mirroring the negotiation leg's window bound.
WINDOW_TICKS = 4
ACTOR = "ci-startup-claim"

# The corrupt first record written into the doomed peer's
# --journal-file before launch: valid JSON that is not a journal
# record, so the startup replay fails by name on line 1 and the bind
# — and with it any field claim — never happens.
CORRUPT_RECORD = (
    '{"ci-pair": "corrupt first record — the doomed-startup induction"}'
)

# The owner token the tamper's stranded claim dies holding — a fresh
# foreign token, never the incumbent's, so the dead claim fences the
# field exactly like the pre-fix doomed startup's orphaned preempt.
STRANDED_OWNER = 0x5A5A

# The journal event kinds the incumbent's record must not gain across
# the doomed attempt — the negotiation leg's disturbance set less the
# command settlement this leg's mid-window receipted command
# legitimately lands.
DISTURBANCE_EVENTS = negotiation.DISTURBANCE_EVENTS - {"command_settled"}


def incumbent_scan(url, failures):
    """One driven scan on the field-owning incumbent — a refused or
    dropped request mid-window is itself the disturbance: a stranded
    foreign claim fences the incumbent's writes at the field."""
    try:
        return simulate.http(f"{url}/scan", {"scans": 1})
    except Exception as error:
        failures.append(
            "the doomed startup disturbed the incumbent — POST /scan "
            f"answered {error}"
        )
        raise Abort


def startup_claim_pass(args, tamper):
    """The startup-claim run: converge, switch, doomed induction,
    window, audit, restore. Returns `(digest_entries, evidence,
    failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the startup-claim "
            "leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    digest_entries, evidence, failures = [], {}, []
    rig = probe_io = foreign = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        plant_io = rig.plant_io
        # The genuinely foreign attachment the fencing probes run on —
        # the run's own plant-protocol client can join a recorded
        # claim's holder set, so it never runs mutation probes.
        probe_io = simulate.PlantClient(rig.plant_addr)
        with open(args.model) as handle:
            model = json.load(handle)
        points = failover.signal_points(model)
        if points is None:
            raise Abort(
                "the emitted model declares no p101-cmd/level-primary "
                "signal points — the leg has no field output to watch"
            )

        # Phase 1 — convergence, then the documented switch: the
        # promoted peer takes the plant's writer claim — the standing
        # claim the doomed startup must never strand or preempt, and
        # the released-controller behavior a fresh launch shows only
        # once a promotion has claimed.
        converged = rig.converge(failures)
        switched = rig.switch(
            duty_url,
            standby_url,
            failures,
            audit_receipts=True,
        )
        incumbent_url, tracker_url = standby_url, duty_url
        owner = switched["owner"]
        evidence["switched_at"] = switched["demote"]["tick"]
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )
        digest_entries.append(
            {
                "phase": "switch",
                "demote": switched["demote"],
                "promote": switched["promote"],
                "ticks": switched["ticks"],
            }
        )

        # Phase 2 — the baseline audit positions the doomed startup
        # must leave untouched: the incumbent's role and advancing
        # tick, its receipted command path and receipt log, the
        # standing claim's fencing verdict, the field output it keeps
        # writing, and its journal position.
        incumbent_role = pair.get(
            f"{incumbent_url}/role", "GET /role", failures
        )
        if incumbent_role.get("role") != "active":
            failures.append(
                f"the promoted field owner reports "
                f"{incumbent_role.get('role')!r}, expected active"
            )
            raise Abort
        held = failover.field_read(plant_io, points["cmd"], failures)[
            "value"
        ]
        image_cmd = simulate.snapshot_point(owner, points["cmd"])
        if held != image_cmd:
            failures.append(
                f"the field carries {held} on the served out-point "
                f"while the incumbent reports {image_cmd} — the "
                "settled pair's writes never landed"
            )
            raise Abort
        probe0 = failover.probe_kind(
            failover.foreign_probe(probe_io, points["cmd"], held)
        )
        if probe0 != "fenced":
            failures.append(
                "the field held no writer claim after the switch — a "
                "foreign attachment's mutation probe answered "
                f"{probe0}, so the leg has no standing claim to "
                "protect"
            )
            raise Abort
        receipts0 = pair.get(
            f"{incumbent_url}/receipts", "GET /receipts", failures
        )
        journal0 = pair.get(
            f"{incumbent_url}/journal", "GET /journal", failures
        )
        digest_entries.append(
            {
                "phase": "baseline",
                "tick": owner["tick"],
                "incumbent_role": incumbent_role,
                "field": held,
                "probe": probe0,
                "receipts": receipts0,
                "journal_entries": len(journal0),
            }
        )

        # Phase 3 — the induction: the third released controller's
        # declared --journal-file gains a first record its startup
        # replay cannot read. The spawn must abort at startup
        # validation — no listener, no claim line — naming the replay
        # failure.
        foreign_dir = os.path.join(rig.scratch, "foreign")
        os.makedirs(foreign_dir)
        foreign_files = {
            "state_file": os.path.join(foreign_dir, "state.json"),
            "journal_file": os.path.join(foreign_dir, "journal.jsonl"),
        }
        with open(foreign_files["journal_file"], "w") as handle:
            handle.write(CORRUPT_RECORD + "\n")
        foreign, foreign_url, preamble = pair.spawn_peer(
            args.controller,
            args.model,
            args.dt,
            rig.plant_addr,
            None,
            foreign_files,
        )
        induction = {
            "exit": foreign.poll(),
            "listening": foreign_url is not None,
            "replay_refused": any(
                "cannot be replayed" in line for line in preamble
            ),
            "claim_logged": any(
                "write-ownership claim held" in line for line in preamble
            ),
        }
        if foreign_url is not None:
            failures.append(
                "the doomed startup served its monitor — the corrupt "
                "first record did not fail its journal replay"
            )
            raise Abort
        if foreign.poll() in (None, 0):
            failures.append(
                f"the doomed startup is still running or exited "
                f"cleanly (exit {foreign.poll()}) — the corrupt "
                "journal never refused it"
            )
            raise Abort
        if not induction["replay_refused"]:
            failures.append(
                "the doomed startup exited without naming the journal "
                f"replay failure: {'; '.join(preamble) or 'no diagnostic'}"
            )
            raise Abort
        if induction["claim_logged"]:
            failures.append(
                "the doomed startup logged the field write-ownership "
                "claim — the preemptive claim ran before the failed "
                f"replay: {'; '.join(preamble)}"
            )
            raise Abort
        digest_entries.append({"phase": "induction", **induction})

        if tamper == "stranded-claim":
            # The pre-fix defect made literal: the doomed startup's
            # preemptive claim lands before its validation fails and
            # outlives the process — a foreign attachment takes the
            # field's claim under a fresh token and drops its hold
            # unreleased, stranding the dead claim over the incumbent
            # exactly like the orphaned preempt did.
            stranded = simulate.PlantClient(rig.plant_addr)
            stranded.request(
                {"op": "claim_writer", "owner": STRANDED_OWNER}
            )
            stranded.close()

        # Phase 4 — the observation window: each round drives the
        # pair's tracking-first tick, then asserts the positions the
        # attempt must never move — roles, images, the advancing tick,
        # the landing field write, the fenced probe — while one
        # kind-declared command submitted to the incumbent mid-window
        # must still receipt accepted.
        schema = pair.get(f"{incumbent_url}/schema", "GET /schema", failures)
        declared_cmd = pair.declared_command(schema)
        if declared_cmd is None:
            failures.append(
                "the served registry declares no command — the leg's "
                "mid-window receipted submission has nothing to "
                "exercise"
            )
            raise Abort
        component, spec = declared_cmd
        command = {
            "invoke": {
                "component": component,
                "command": spec["name"],
                "arguments": simulate.command_arguments(spec),
            }
        }
        window = []
        submission = None
        previous = owner["tick"]
        for round_no in range(WINDOW_TICKS):
            tracked = pair.scan(tracker_url, failures)
            owner = incumbent_scan(incumbent_url, failures)
            # The carried-command one-tick lag (issue #689): the
            # tracker's quiesced scan carries the adopted receipt
            # instead of settling it, so its image trails the
            # incumbent's by the command's effect until the following
            # pull. The window absorbs exactly that — one more
            # tracking-first tick must reconverge the pair.
            if pair.select_snapshot(tracked) != pair.select_snapshot(owner):
                tracked = pair.scan(tracker_url, failures)
                owner = incumbent_scan(incumbent_url, failures)
                if pair.select_snapshot(tracked) != pair.select_snapshot(
                    owner
                ):
                    failures.append(
                        "the doomed startup disturbed the pair — the "
                        "peers' images diverged at tick "
                        f"{owner['tick']}"
                    )
                    raise Abort
            incumbent_role = pair.get(
                f"{incumbent_url}/role", "GET /role", failures
            )
            if incumbent_role.get("role") != "active":
                failures.append(
                    "the doomed startup disturbed the incumbent — it "
                    "left role=active: GET /role answers "
                    f"{incumbent_role}"
                )
                raise Abort
            tracker_role = pair.get(
                f"{tracker_url}/role", "GET /role", failures
            )
            sync = tracker_role.get("sync")
            if tracker_role.get("role") != "standby" or not (
                isinstance(sync, dict) and "tracking" in sync
            ):
                failures.append(
                    "the doomed startup disturbed the pair — the "
                    "tracking peer reported a spurious role change: "
                    f"{tracker_role}"
                )
                raise Abort
            if owner["tick"] <= previous:
                failures.append(
                    "the doomed startup disturbed the incumbent — its "
                    f"tick stalled at {owner['tick']}"
                )
                raise Abort
            previous = owner["tick"]
            held = failover.field_read(plant_io, points["cmd"], failures)[
                "value"
            ]
            image_cmd = simulate.snapshot_point(owner, points["cmd"])
            if held != image_cmd:
                failures.append(
                    "the doomed startup disturbed the incumbent — its "
                    f"writes stopped landing: the field carries {held} "
                    f"while its image reports {image_cmd}"
                )
                raise Abort
            probe = failover.probe_kind(
                failover.foreign_probe(probe_io, points["cmd"], held)
            )
            if probe != "fenced":
                failures.append(
                    "the field's fencing changed across the doomed "
                    "startup — a foreign attachment's probe answered "
                    f"{probe}, expected fenced under the standing "
                    "claim"
                )
                raise Abort
            if submission is None:
                status, receipt = pair.request(
                    f"{incumbent_url}/command",
                    {"command": command, "actor": ACTOR},
                )
                submission = {"status": status, "receipt": receipt}
                if (
                    status != 200
                    or simulate.receipt_outcome(receipt) != "accepted"
                ):
                    failures.append(
                        "the doomed startup disturbed the incumbent — "
                        "the mid-window command answered "
                        f"{status} {receipt}, expected an accepted "
                        "receipt"
                    )
                    raise Abort
            window.append(
                {
                    "round": round_no,
                    "tick": owner["tick"],
                    "incumbent": "active",
                    "tracker": "tracking",
                    "probe": probe,
                    "field": held,
                }
            )
        digest_entries.append(
            {
                "phase": "window",
                "command": command,
                "submission": submission,
                "window": window,
            }
        )

        # Phase 5 — the post-attempt audit: the mid-window command
        # settled applied on both peers, the incumbent's journal
        # gained no disturbance records, and the doomed peer's files
        # prove the run never survived its failed replay — the journal
        # still exactly the corrupt record, the state file never
        # written.
        receipts1 = pair.get(
            f"{incumbent_url}/receipts", "GET /receipts", failures
        )
        settled = [
            entry
            for entry in receipts1
            if entry.get("command") == command
            and simulate.receipt_outcome(entry) == "applied"
        ]
        if not settled:
            failures.append(
                "the incumbent's receipt log never carried the "
                "mid-window command's applied settlement"
            )
            raise Abort
        receipts_tracker = pair.get(
            f"{tracker_url}/receipts", "GET /receipts", failures
        )
        if receipts_tracker != receipts1:
            failures.append(
                "the peers' receipt logs diverged across the doomed "
                "startup"
            )
            raise Abort
        journal1 = pair.get(
            f"{incumbent_url}/journal", "GET /journal", failures
        )
        if journal1[: len(journal0)] != journal0:
            failures.append(
                "the incumbent's journal no longer answers its "
                "pre-attempt entries verbatim"
            )
            raise Abort
        leaked = [
            entry
            for entry in journal1[len(journal0) :]
            if DISTURBANCE_EVENTS & set(entry.get("event", {}))
        ]
        if leaked:
            failures.append(
                "the incumbent's journal gained disturbance records "
                f"during the doomed startup: {leaked}"
            )
            raise Abort
        with open(foreign_files["journal_file"]) as handle:
            journal_after = handle.read()
        if journal_after != CORRUPT_RECORD + "\n":
            failures.append(
                "the doomed startup ran past the failed replay — its "
                f"journal file gained records: {journal_after[:300]}"
            )
            raise Abort
        if os.path.exists(foreign_files["state_file"]):
            failures.append(
                "the doomed startup persisted a checkpoint — it ran "
                "past the failed journal replay"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "audit",
                "receipts": receipts1,
                "journal_added": journal1[len(journal0) :],
            }
        )

        # Phase 6 — teardown: the foreign process is long dead, and
        # the pair returns to its launch roles — the documented switch
        # back proves the attempt left the deployed pair whole.
        pair.stop(foreign)
        foreign = None
        restored = rig.switch(
            incumbent_url,
            tracker_url,
            failures,
            demote_what="the incumbent",
            promote_what="the tracking peer",
            audit_receipts=True,
        )
        evidence["restored_at"] = restored["promote"]["tick"]
        digest_entries.append(
            {
                "phase": "restore",
                "demote": restored["demote"],
                "promote": restored["promote"],
                "ticks": restored["ticks"],
                "duty_role": restored["promoted_role"],
                "standby_role": restored["demoted_role"],
            }
        )
        evidence["final_tick"] = restored["owner"]["tick"]
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        pair.stop(foreign)
        if probe_io is not None:
            probe_io.close()
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
        choices=["stranded-claim"],
        help="strand a dead foreign claim over the incumbent — the "
        "pre-fix defect's observable shape — so the pass must fail "
        "naming the disturbance it saw",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = startup_claim_pass(
            args, args.tamper
        )
    except Abort as abort:
        for line in abort.args:
            eprint(f"startup-claim: {line}")
        return 1
    for failure in failures:
        eprint(f"startup-claim: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"startup-claim: the {args.tamper} case passed silently "
                "— the leg never noticed the stranded claim"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"startup-claim-digest {digest} — switched at tick "
        f"{evidence['switched_at']}, the doomed startup refused at "
        "journal replay, the incumbent held role=active through the "
        f"window, launch roles restored at tick "
        f"{evidence['restored_at']}, run ended at tick "
        f"{evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
