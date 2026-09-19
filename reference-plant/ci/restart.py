#!/usr/bin/env python3
"""The restart-recovery leg for the reference plant — the
lone-controller half of WW-LCM-001's restart evidence (decisions 35
and 36), run entirely on the released tooling.

The `deploy` stage proves the manifest and the rig definition agree on
the declared `state_file`/`journal_file` persistence paths and their
writable mounts; this leg proves the recovery contract itself.
`dcs-plant-server` serves the checked-in model and dynamics while
`dcs-controller --driven --remote` launches with the manifest-declared
`--state-file`/`--journal-file` flags pointed at runner-owned scratch
paths, runs the deterministic scenario past a leg boundary — far
enough to leave applied receipts and journaled transitions — is
stopped, and relaunches onto the same files. The resumed run must:

- report its resume on stderr and continue at the persisted tick —
  never a silent cold start at tick zero;
- reproduce the uninterrupted reference pass: identical per-leg
  outcomes, receipt outcomes, and served field image;
- serve the replayed pre-restart journal verbatim behind the restart's
  `run_boundary` entry — the file marker's served form, carrying the
  restored tick — and continue the durable journal's `seq` order
  across it, the run's settled receipts and standing census never
  re-journaling.

A state file gone missing is caught by those same assertions: the
relaunch reports no resume — the release contract's documented cold
start — and the leg fails naming the loss rather than passing a silent
tick-zero restart. An unparseable file fails the relaunch's startup
with the controller's named refusal, which the leg reports verbatim.

Usage:

    restart.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `restart-digest <sha256>` line prints — the check runs
two passes and compares them. A contract violation reports
`restart: …` lines on stderr and exits 1; the check reports that as
`restart-resume-failed`. `--tamper missing-state-file` and
`--tamper corrupt-state-file` doctor the state file at the restart
point so the check can prove the leg's diagnostics fire — a doctored
pass must exit nonzero carrying its evidence.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile

import pair
import simulate


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class MonitorRef:
    """The monitor base URL `run_legs` addresses, boxed so a mid-run
    relaunch retargets it — each request reads the current URL."""

    def __init__(self):
        self.url = None

    def __str__(self):
        return self.url


def check_journal(records, persisted, failures):
    """The file's contract across the restart: exactly the two
    run-boundary markers — run 1 cold at tick 0, run 2 at the restored
    tick — and entry seqs continuing 1..n in file order across them."""
    boundaries = [record for kind, record in records if kind == "boundary"]
    if boundaries != [{"run": 1, "tick": 0}, {"run": 2, "tick": persisted}]:
        failures.append(
            f"the journal file's run boundaries are {boundaries}, "
            f"expected run 1 at tick 0 and run 2 at the persisted "
            f"tick {persisted}"
        )
        return
    seqs = [record["seq"] for kind, record in records if kind == "entry"]
    if seqs != list(range(1, len(seqs) + 1)):
        failures.append(f"the journal file's entry seqs are not 1..n in order: {seqs}")


def manifest_persistence(path):
    """The field-owning controller's declared persistence file names —
    the basenames the leg instantiates under its runner-owned scratch
    directory. Returns None when the manifest declares no persistence
    for the controller that carries no standby peer."""
    with open(path) as handle:
        manifest = json.load(handle)
    duty = next(
        (
            controller
            for controller in manifest.get("controllers", [])
            if "standby" not in controller
        ),
        None,
    )
    if duty is None:
        return None
    state_file = duty.get("state_file")
    journal_file = duty.get("journal_file")
    if not state_file or not journal_file:
        return None
    return {
        "state_file": os.path.basename(state_file),
        "journal_file": os.path.basename(journal_file),
    }


def interrupted_pass(args, scenario, persistence, tamper):
    """The restart run: the scenario driven on the persistent rig with
    the controller stopped and relaunched onto the same files at the
    midpoint leg boundary. Returns `(digest_entries, evidence,
    failures)`."""
    legs = scenario["legs"]
    mid = len(legs) // 2
    scratch = tempfile.mkdtemp(prefix="dcs-restart-")
    state_file = os.path.join(scratch, persistence["state_file"])
    journal_file = os.path.join(scratch, persistence["journal_file"])
    digest_entries, evidence, failures = [], {}, []
    monitor = MonitorRef()
    plant = controller = client = None
    try:
        plant, plant_addr = pair.spawn_plant(
            args.plant_server, args.model, args.dynamics
        )
        client = simulate.PlantClient(plant_addr)

        def launch():
            nonlocal controller
            controller, monitor.url, preamble = pair.spawn_peer(
                args.controller,
                args.model,
                scenario["dt"],
                plant_addr,
                None,
                {"state_file": state_file, "journal_file": journal_file},
            )
            return preamble

        preamble = launch()
        if monitor.url is None:
            raise Abort(
                "the first launch exited at startup: "
                f"{'; '.join(preamble) or 'no diagnostic'}"
            )

        def between(index):
            """At the midpoint leg boundary: stop the controller,
            assert the persisted checkpoint and the run-1 journal, then
            relaunch onto the same files and assert the resume."""
            nonlocal controller
            if index != mid:
                return
            stopped = simulate.http(f"{monitor}/snapshot")
            served = simulate.http(f"{monitor}/journal")
            pair.stop(controller)

            if not os.path.exists(state_file):
                failures.append(
                    f"the driven run left no state file at {state_file}"
                )
                raise Abort
            try:
                with open(state_file) as handle:
                    checkpoint = json.load(handle)
            except (OSError, json.JSONDecodeError) as error:
                failures.append(
                    f"the persisted state file does not parse: {error}"
                )
                raise Abort
            persisted = checkpoint.get("tick")
            evidence["persisted_tick"] = persisted
            if persisted != stopped["tick"]:
                failures.append(
                    f"the state file persisted tick {persisted} while "
                    f"the run stood at {stopped['tick']}"
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
            entries = [record for kind, record in records if kind == "entry"]
            seqs = [entry["seq"] for entry in entries]
            if seqs != list(range(1, len(seqs) + 1)):
                failures.append(
                    f"run 1's journal seqs are not 1..n in order: {seqs}"
                )
                raise Abort
            if not any(
                "applied"
                in entry["event"]
                .get("command_settled", {})
                .get("receipt", {})
                .get("outcome", {})
                for entry in entries
            ):
                failures.append(
                    "run 1 left no applied receipt in the journal — the "
                    "restart point precedes the evidence it exists to keep"
                )
                raise Abort
            evidence["run1_entries"] = len(entries)

            if tamper == "missing-state-file":
                os.remove(state_file)
            elif tamper == "corrupt-state-file":
                with open(state_file, "w") as handle:
                    handle.write("{ not a checkpoint\n")

            preamble = launch()
            if monitor.url is None:
                detail = "; ".join(preamble[-2:]) or "no diagnostic"
                failures.append(
                    f"the relaunched controller exited at startup: {detail}"
                )
                raise Abort
            line = next(
                (
                    line
                    for line in preamble
                    if "resumed from state file" in line
                ),
                None,
            )
            if line is None:
                failures.append(
                    "the relaunched controller never reported a resume — "
                    "its cold start at tick 0 silently loses the "
                    f"persisted run at tick {persisted}"
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
            snapshot = simulate.http(f"{monitor}/snapshot")
            if snapshot["tick"] != persisted:
                failures.append(
                    f"the resumed run reports tick {snapshot['tick']}, "
                    f"the persisted tick is {persisted}"
                )
                raise Abort
            replayed = simulate.http(f"{monitor}/journal")
            if replayed[: len(served)] != served:
                failures.append(
                    "the replayed journal no longer answers the "
                    "pre-restart entries verbatim"
                )
                raise Abort
            boundary = {
                "seq": len(served) + 1,
                "tick": persisted,
                "event": {"run_boundary": {"run": 2}},
            }
            if replayed[len(served) :] != [boundary]:
                failures.append(
                    "the resumed run's served journal does not open "
                    f"with the run-2 boundary entry {boundary}: "
                    f"{replayed[len(served):]}"
                )
                raise Abort
            evidence["replayed_entries"] = len(replayed)
            check_journal(pair.journal_records(journal_file), persisted, failures)
            if failures:
                raise Abort

        digest_entries, leg_failures = simulate.run_legs(
            monitor, client, legs, between_legs=between
        )
        failures += leg_failures
        if not failures:
            records = pair.journal_records(journal_file)
            check_journal(records, evidence["persisted_tick"], failures)
            evidence["journal_entries"] = sum(
                1 for kind, _record in records if kind == "entry"
            )
            evidence["final"] = pair.select_snapshot(
                simulate.http(f"{monitor}/snapshot")
            )
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        pair.stop(controller)
        if client is not None:
            client.close()
        pair.stop(plant)
        shutil.rmtree(scratch, ignore_errors=True)
    return digest_entries, evidence, failures


def reference_pass(args, scenario):
    """The uninterrupted run: the scenario through `simulate`'s plain
    driven rig — what the resumed run must equal."""
    with simulate.driven_rig(
        args.plant_server,
        args.controller,
        args.model,
        args.dynamics,
        scenario["dt"],
    ) as (plant_addr, monitor):
        client = simulate.PlantClient(plant_addr)
        entries, failures = simulate.run_legs(monitor, client, scenario["legs"])
        snapshot = simulate.http(f"{monitor}/snapshot")
        client.close()
    return entries, failures, pair.select_snapshot(snapshot)


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
        choices=["missing-state-file", "corrupt-state-file"],
        help="doctor the state file at the restart point — the pass "
        "must fail naming the evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        scenario = json.load(handle)
    persistence = manifest_persistence(args.manifest)
    if persistence is None:
        eprint(
            "restart: the manifest's field-owning controller declares no "
            "state_file/journal_file — the restart leg has nothing to exercise"
        )
        return 1

    entries, evidence, failures = interrupted_pass(
        args, scenario, persistence, args.tamper
    )
    if args.tamper is not None:
        if not failures:
            eprint(
                f"restart: the {args.tamper} case passed silently — the "
                "leg never noticed the lost checkpoint"
            )
        for failure in failures:
            eprint(f"restart: {failure}")
        return 1

    if not failures:
        reference, reference_snapshot = [], None
        try:
            reference, reference_failures, reference_snapshot = reference_pass(
                args, scenario
            )
        except Exception as error:
            reference_failures = [f"the run raised {error!r}"]
        failures += [f"reference pass: {f}" for f in reference_failures]
        if not failures:
            if entries != reference:
                failures.append(
                    "the resumed run's leg outcomes and receipts diverge "
                    "from the uninterrupted reference pass"
                )
            if evidence["final"] != reference_snapshot:
                failures.append(
                    "the resumed run's final field image diverges from "
                    "the uninterrupted reference pass"
                )
    for failure in failures:
        eprint(f"restart: {failure}")
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(
            {"legs": entries, "evidence": evidence}, sort_keys=True
        ).encode()
    ).hexdigest()
    print(
        f"restart-digest {digest} — resumed at tick "
        f"{evidence['resumed_tick']}, {evidence['journal_entries']} "
        "journal entries across the run boundary"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
