#!/usr/bin/env python3
"""The role-gated refusal leg for the reference plant — the
consumer-side proof that the deployed redundant pair refuses honestly
at its role boundaries (WW-ENG-003, WW-LCM-001).

The pair leg (`ci/pair.py`) proves the manifest-declared pair runs and
switches; this leg proves the refusal half of the same contract on the
same declared deployment: the rig reads the standby wiring and
persistence fields out of `deploy/manifest.json` and spawns the
released tooling exactly as that leg does. The run:

- induction — the freshly launched standby has not yet completed its
  first transfer: no `POST /scan` has driven a checkpoint pull, so
  `GET /role` reports `standby`/`unsynchronized`. `POST /promote` on it
  answers the named `409 not_converged` refusal and no field hand-off
  occurs: the duty stays `active`, the standby stays `standby` and
  unsynchronized, and neither peer's journal carries a `role_changed`
  record;
- convergence — the pair leg's driven-tick loop converges the standby
  to `tracking`, the peers' served snapshots identical, the field
  owner's writes landing undisturbed through the refused promote;
- the role-gated command refusal — a receipted `write_value` against a
  declared writable point submitted to the tracking standby's monitor
  answers the named `not_active` rejection: the point stands unchanged
  in the active's served snapshot, the write is absent from both
  peers' adopted receipt logs, and no `command_settled` journal entry
  on either peer — served or durable — records the write as anything
  but the named refusal: the active's record carries no trace of it at
  all, the standby's echoing the rejection alone;
- the field stays the active's — the plant's stored samples for the
  channel-bound `out` points equal the active's served out image and
  keep advancing while the refusals stand;
- once tracking, the same `POST /promote` the induction refused
  succeeds through the documented switch — `POST /demote` on the field
  owner, then the standby's promoting report — the run continuing
  bumplessly with the promoted peer `active`, the demoted peer
  reconverged `tracking`, and the field image still the owner's.

Usage:

    refusal.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `refusal-digest <sha256>` line prints — the check runs
two passes and compares them (`refusal-nondeterministic`). A contract
violation reports `refusal: …` lines on stderr and exits 1 — the
check's `refusal-failed`. `--tamper expect-applied` doctors the leg's
own expectation — asserting the standby-directed write settles
`applied` — so the leg proves its refusal assertion fires rather than
passing an unexercised contract.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

import pair
import simulate


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The driven ticks each phase runs and the actor the leg's receipted
# submission declares — the same counts the pair leg converges and
# hands over on.
CONVERGE_TICKS = pair.CONVERGE_TICKS
HANDOVER_TICKS = pair.HANDOVER_TICKS
ACTOR = "ci-refusal"


def command_receipts(entries, command):
    """The `command_settled` receipts a journal-entry list carries for
    one submitted command — the audit a write leaves behind on the
    peer that recorded it."""
    return [
        entry["event"]["command_settled"]["receipt"]
        for entry in entries
        if "command_settled" in entry.get("event", {})
        and entry["event"]["command_settled"].get("receipt", {}).get("command")
        == command
    ]


def journal_file_command_receipts(path, command):
    """The same audit in a peer's durable `--journal-file`."""
    return command_receipts(
        [
            record
            for kind, record in pair.journal_records(path)
            if kind == "entry"
        ],
        command,
    )


def field_out_samples(client):
    """The simulated plant's stored samples for its `out` points — the
    field image the field-owning peer's scans keep writing, keyed by
    point."""
    response = client.request({"op": "list_points"})
    points = response.get("points") if isinstance(response, dict) else None
    if not isinstance(points, list):
        raise Abort(f"the simulated plant's list_points answered {response}")
    return {
        entry["point"]: entry.get("sample")
        for entry in points
        if isinstance(entry, dict) and entry.get("direction") == "out"
    }


def field_mismatches(snapshot, samples):
    """Each field `out` point whose plant-stored value differs from the
    field owner's served image — the field holding another writer's
    values, or no writer's at all."""
    image = {
        entry["point"]: entry.get("sample")
        for entry in snapshot.get("points", [])
        if entry.get("direction") == "out"
    }
    mismatches = []
    for point, sample in sorted(samples.items()):
        served = image.get(point)
        value = served.get("value") if isinstance(served, dict) else None
        if not isinstance(sample, dict) or sample.get("value") != value:
            mismatches.append(
                f"field point {point} stores {sample}, the owner's "
                f"image serves {served}"
            )
    return mismatches


def refusal_pass(args, tamper):
    """The refusal run: induction refusal, converge, command refusal,
    switch, continue. Returns `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the refusal leg "
            "has nothing to exercise"
        )
    manifest, duty_decl, standby_decl = declared
    with open(args.model) as handle:
        model = json.load(handle)
    writable = [
        point["id"]
        for point in sorted(model["io_points"], key=lambda entry: entry["id"])
        if point.get("writable") and point["value_type"] == "bool"
    ]
    if not writable:
        raise Abort(
            "the emitted model declares no writable boolean point — "
            "the role-gated write leg has nothing to exercise"
        )
    scratch = tempfile.mkdtemp(prefix="dcs-refusal-")
    digest_entries, evidence, failures = [], {}, []
    plant = duty = standby = None
    try:

        def persistence(entry):
            """The controller's declared persistence file basenames
            instantiated under the leg's runner-owned scratch directory
            — the same manifest fields the pair leg honors."""
            root = os.path.join(scratch, entry["name"])
            os.makedirs(root, exist_ok=True)
            return {
                field: os.path.join(root, os.path.basename(entry[field]))
                if entry.get(field)
                else None
                for field in ("state_file", "journal_file")
            }

        duty_files = persistence(duty_decl)
        standby_files = persistence(standby_decl)

        plant = subprocess.Popen(
            [
                args.plant_server,
                args.model,
                "--dynamics",
                args.dynamics,
                "--listen",
                "127.0.0.1:0",
            ],
            stderr=subprocess.PIPE,
            text=True,
        )
        plant_addr = simulate.listen_address(plant, "dcs-plant-server")
        plant_io = simulate.PlantClient(plant_addr)

        duty, duty_url, preamble = pair.spawn_peer(
            args.controller, args.model, args.dt, plant_addr, None, duty_files
        )
        if duty_url is None:
            raise Abort(
                f"the duty controller {duty_decl['name']} exited at "
                f"startup: {'; '.join(preamble) or 'no diagnostic'}"
            )
        standby, standby_url, preamble = pair.spawn_peer(
            args.controller,
            args.model,
            args.dt,
            plant_addr,
            duty_url.removeprefix("http://"),
            standby_files,
        )
        if standby_url is None:
            raise Abort(
                f"the standby controller {standby_decl['name']} exited at "
                f"startup: {'; '.join(preamble) or 'no diagnostic'}"
            )

        # Phase 1 — the induction: the freshly launched standby has not
        # yet completed its first transfer — no `POST /scan` has driven
        # a checkpoint pull, so it stands `unsynchronized`. `POST
        # /promote` on it must answer the named `not_converged` refusal
        # and hand nothing off: the duty stays `active`, the standby
        # stays `standby` and unsynchronized, and neither peer's
        # journal carries a `role_changed` record.
        standby_role = pair.get(f"{standby_url}/role", "GET /role", failures)
        if standby_role.get("role") != "standby" or (
            standby_role.get("sync") != "unsynchronized"
        ):
            failures.append(
                "the induction's freshly launched standby reports "
                f"{standby_role} — expected standby/unsynchronized"
            )
            raise Abort
        status, refused = pair.request(f"{standby_url}/promote", {})
        reason = refused.get("not_converged") if isinstance(refused, dict) else None
        if status != 409 or reason is None:
            failures.append(
                f"POST /promote on the pre-transfer standby answered "
                f"{status} {refused}, expected 409 not_converged"
            )
            raise Abort
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        if duty_role.get("role") != "active":
            failures.append(
                f"the field owner reports {duty_role.get('role')!r} "
                "after the refused promote, expected active — the "
                "refusal handed the field off"
            )
        standby_after = pair.get(f"{standby_url}/role", "GET /role", failures)
        if standby_after != standby_role:
            failures.append(
                f"the refused promote changed the standby's report to "
                f"{standby_after} from {standby_role}"
            )
        peers = {
            duty_decl["name"]: (duty_url, duty_files),
            standby_decl["name"]: (standby_url, standby_files),
        }
        for name, (url, _files) in peers.items():
            journal = pair.get(f"{url}/journal", "GET /journal", failures)
            if pair.role_transitions(journal):
                failures.append(
                    f"{name}'s journal carries role transitions after "
                    "the refused promote — a field hand-off occurred"
                )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "induction",
                "standby_role": standby_role,
                "refused_promote": refused,
                "duty_role": duty_role,
                "standby_after": standby_after,
            }
        )

        # Phase 2 — convergence: the pair leg's driven-tick loop, the
        # tracking peer scanned first so each of its pulls applies the
        # owner's latest checkpoint and the peers rest at the same tick
        # with identical images — the field owner's writes landing
        # undisturbed through the refused promote.
        ticks = []
        for _ in range(CONVERGE_TICKS):
            tracked = pair.scan(standby_url, failures)
            owner = pair.scan(duty_url, failures)
            if pair.select_snapshot(tracked) != pair.select_snapshot(owner):
                failures.append(
                    "the tracking peer's image diverged from the field "
                    f"owner's at tick {owner['tick']}"
                )
                raise Abort
            ticks.append(owner["tick"])
        standby_role = pair.get(f"{standby_url}/role", "GET /role", failures)
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        sync = standby_role.get("sync")
        if standby_role.get("role") != "standby" or not (
            isinstance(sync, dict) and "tracking" in sync
        ):
            failures.append(
                "the tracking peer never reported tracking — GET /role "
                f"answers {standby_role}"
            )
            raise Abort
        if duty_role.get("role") != "active":
            failures.append(
                f"the field owner reports {duty_role.get('role')!r}, "
                "expected active"
            )
            raise Abort
        field = field_out_samples(plant_io)
        for mismatch in field_mismatches(owner, field):
            failures.append(
                f"{mismatch} after the refused promote — the field "
                "does not hold the active's writes"
            )
        if failures:
            raise Abort
        evidence["converged"] = ticks[-1]
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": ticks,
                "duty_role": duty_role,
                "standby_role": standby_role,
                "field": field,
            }
        )

        # Phase 3 — the role-gated command refusal: a receipted
        # `write_value` against the declared writable point, submitted
        # to the tracking standby's monitor, must answer the named
        # `not_active` rejection — the point unchanged in the active's
        # served snapshot, the write absent from both peers' adopted
        # receipt logs, and no journal entry on either peer recording
        # it as anything but the named refusal.
        point = writable[0]
        before = simulate.snapshot_point(owner, point)
        write = {
            "write_value": {
                "kind": "bool",
                "point": point,
                "value": {"bool": not before["bool"]},
            }
        }
        status, receipt = pair.request(
            f"{standby_url}/command", {"command": write, "actor": ACTOR}
        )
        reason = (
            receipt.get("outcome", {}).get("rejected", {}).get("reason", {})
            if isinstance(receipt, dict)
            else {}
        )
        if tamper == "expect-applied":
            # The doctored expectation: the leg asserts the write
            # settled applied — the standby's honest not_active
            # rejection must fail it, naming the actual answer.
            if status != 200 or simulate.receipt_outcome(receipt) != "applied":
                failures.append(
                    f"the standby-directed write answered {status} "
                    f"{receipt}, expected an applied receipt"
                )
                raise Abort
        elif status != 200 or "not_active" not in reason:
            failures.append(
                f"the standby's role boundary answered {status} "
                f"{receipt}, expected a rejected not_active receipt"
            )
            raise Abort
        # The settling tick: the peers' images must stay identical and
        # the point unchanged in the active's served snapshot.
        tracked = pair.scan(standby_url, failures)
        owner = pair.scan(duty_url, failures)
        if pair.select_snapshot(tracked) != pair.select_snapshot(owner):
            failures.append(
                "the tracking peer's image diverged from the field "
                f"owner's at tick {owner['tick']} — the refused write "
                "reached a peer's state"
            )
        if simulate.snapshot_point(owner, point) != before:
            failures.append(
                f"point {point} reads {simulate.snapshot_point(owner, point)} "
                f"in the active's snapshot after the refused write, "
                f"expected {before} — the write reached the active"
            )
        for name, (url, _files) in peers.items():
            receipts = pair.get(f"{url}/receipts", "GET /receipts", failures)
            if any(
                entry.get("command") == write for entry in receipts
            ):
                failures.append(
                    f"{name}'s adopted receipt log carries the refused "
                    "write — the pair's command audit recorded a "
                    "rejected command as submitted"
                )
        audit = {}
        for name, (url, files) in peers.items():
            served = pair.get(f"{url}/journal", "GET /journal", failures)
            records = {"served": command_receipts(served, write)}
            journal_path = files.get("journal_file")
            if journal_path is not None:
                if not os.path.exists(journal_path):
                    failures.append(
                        f"{name}'s declared journal file {journal_path} "
                        "does not exist — the --journal-file flag was "
                        "not honored"
                    )
                else:
                    records["file"] = journal_file_command_receipts(
                        journal_path, write
                    )
            audit[name] = records
        for name, records in audit.items():
            for source, receipts_ in records.items():
                if name == duty_decl["name"] and receipts_:
                    failures.append(
                        f"the active's {source} journal carries a "
                        f"command record for the refused write: "
                        f"{receipts_} — the write reached the field "
                        "owner's audit"
                    )
                leaked = [
                    entry
                    for entry in receipts_
                    if simulate.receipt_outcome(entry) != "not_active"
                ]
                if leaked:
                    failures.append(
                        f"{name}'s {source} journal records the refused "
                        f"write as something but the named rejection: "
                        f"{leaked}"
                    )
        if failures:
            raise Abort
        field = field_out_samples(plant_io)
        for mismatch in field_mismatches(owner, field):
            failures.append(
                f"{mismatch} after the refused write — the field does "
                "not hold the active's writes"
            )
        if failures:
            raise Abort
        evidence["write_at"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "command",
                "point": point,
                "write": write,
                "receipt": receipt,
                "field": field,
                "audit": audit,
            }
        )

        # Phase 4 — once tracking, the same `POST /promote` the
        # induction refused succeeds through the documented switch:
        # demote the field owner, then promote the converged standby —
        # each answered by its RoleReport — and the run continues
        # bumplessly with the promoted peer active and the demoted peer
        # reconverging tracking.
        status, demote = pair.request(f"{duty_url}/demote", {})
        if status != 200 or demote.get("role") != "demoting":
            failures.append(
                f"POST /demote on the field owner answered {status} "
                f"{demote}, expected a demoting report"
            )
            raise Abort
        status, promote = pair.request(f"{standby_url}/promote", {})
        if status != 200 or promote.get("role") != "promoting":
            failures.append(
                f"POST /promote on the tracking standby answered "
                f"{status} {promote}, expected a promoting report — "
                "the request the induction refused"
            )
            raise Abort
        evidence["switched_at"] = demote["tick"]
        ticks = []
        for _ in range(HANDOVER_TICKS):
            tracked = pair.scan(duty_url, failures)
            owner = pair.scan(standby_url, failures)
            if pair.select_snapshot(tracked) != pair.select_snapshot(owner):
                failures.append(
                    "the demoted peer's image diverged from the "
                    f"promoted owner's at tick {owner['tick']} — the "
                    "switch was not bumpless"
                )
                raise Abort
            ticks.append(owner["tick"])
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        standby_role = pair.get(f"{standby_url}/role", "GET /role", failures)
        if standby_role.get("role") != "active":
            failures.append(
                f"the promoted peer reports {standby_role.get('role')!r}, "
                "expected active"
            )
            raise Abort
        sync = duty_role.get("sync")
        if duty_role.get("role") != "standby" or not (
            isinstance(sync, dict) and "tracking" in sync
        ):
            failures.append(
                "the demoted peer never reconverged — GET /role "
                f"answers {duty_role}"
            )
            raise Abort
        field = field_out_samples(plant_io)
        for mismatch in field_mismatches(owner, field):
            failures.append(
                f"{mismatch} after the switch — the field does not "
                "hold the promoted owner's writes"
            )
        expected = {
            duty_decl["name"]: [("active", "demoting"), ("demoting", "standby")],
            standby_decl["name"]: [
                ("standby", "promoting"),
                ("promoting", "active"),
            ],
        }
        transitions = {}
        for name, (url, _files) in peers.items():
            journal = pair.get(f"{url}/journal", "GET /journal", failures)
            transitions[name] = pair.role_transitions(journal)
            if [
                (frm, to) for _tick, frm, to in transitions[name]
            ] != expected[name]:
                failures.append(
                    f"{name}'s served journal carries the role "
                    f"transitions {transitions[name]}, expected "
                    f"{expected[name]} — the switch's hand-off record"
                )
        if failures:
            raise Abort
        evidence["final_tick"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "switch",
                "demote": demote,
                "promote": promote,
                "ticks": ticks,
                "duty_role": duty_role,
                "standby_role": standby_role,
                "field": field,
                "transitions": transitions,
            }
        )
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        pair.stop(standby)
        pair.stop(duty)
        pair.stop(plant)
        shutil.rmtree(scratch, ignore_errors=True)
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
        choices=["expect-applied"],
        help="doctor the leg's own expectation — the pass must fail "
        "naming the standby's actual answer",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = refusal_pass(args, args.tamper)
    except Abort as abort:
        for line in abort.args:
            eprint(f"refusal: {line}")
        return 1
    for failure in failures:
        eprint(f"refusal: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"refusal: the {args.tamper} case passed silently — the "
                "leg never noticed the standby's honest answer"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    point = digest_entries[2]["point"]
    print(
        f"refusal-digest {digest} — promote refused not_converged "
        f"before the first transfer, write on point {point} refused "
        f"not_active, tracking by tick {evidence['converged']}, "
        f"promoted at tick {evidence['switched_at']}, run continued to "
        f"tick {evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
