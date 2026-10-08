#!/usr/bin/env python3
"""The configuration-backup and restore leg for the reference plant —
the consumer-side backup drill `docs/releases/backup-record.md`
declares (WW-LCM-002's backup-and-restore clause), run entirely on the
released tooling.

Sector guidance makes backup creation, isolation, and testing the
utility's own program while the platform contributes the enumerated
artifact set — the model document, the dynamics document, the
per-controller state and journal files, and the manifest pin. This leg
proves the *deployed* drill the same way the restart leg proves the
lone-controller recovery: the manifest-declared pair runs the
deterministic scenario far enough to leave applied receipts and
journaled transitions, the drill backs the running pair up, wipes the
pair's volumes, restores the backup set onto fresh volumes, and the
resumed pair continues at the persisted tick. The run:

- converges the manifest-declared pair to `tracking` and submits one
  receipted `write_value`, so the backup has operator state, receipts,
  and journal transitions to keep;
- assembles the backup set beside the live deployment — the model and
  dynamics documents, the deployment manifest, and each declared
  controller's state, journal, and history files — recording the
  backup record (`backup.json`): the `dcs_release` pin, the model and
  dynamics fingerprints, and every file's sha256;
- audits the backup's completeness before any restore runs: a backup
  missing a named artifact fails naming it, never restoring silently
  from a partial set;
- wipes the pair's volumes — both controllers stopped, their
  persistence files deleted — and restores the backup set onto fresh
  volumes;
- relaunches the pair onto the restored files: each peer must report
  its resume at its persisted tick — never a silent cold start — serve
  the pre-wipe receipt log verbatim, replay the pre-wipe journal
  behind the restart's `run_boundary` entry with the durable `seq`
  order continuing across it, reconverge to `active` + `tracking`,
  and continue the run bumplessly with identical images.

Usage:

    backup_restore.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `backup-restore-digest <sha256>` line prints — the
check runs two passes and compares them
(`backup-restore-nondeterministic`). A contract violation reports
`backup-restore: …` lines on stderr and exits 1 — the check's
`backup-restore-failed`. `--tamper missing-state-file` and `--tamper
missing-journal-file` drop the named artifact from the assembled
backup, so the check can prove the completeness audit fires — a
doctored pass must exit nonzero naming the missing artifact.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import pair
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored cases: a backup missing a named artifact must fail the
# completeness audit naming it — never a silent partial restore.
LEG = {
    "order": 893,
    "title": "the configuration-backup and restore leg",
    "passes": "backup-restore-leg",
    "tampers": [
        {
            "name": "missing-state-file",
            "passed": "a backup missing a state file passed the backup leg",
            "missed": "the missing-state-file case did not report its named diagnostic",
            "evidence": ["the backup carries no state file"],
        },
        {
            "name": "missing-journal-file",
            "passed": "a backup missing a journal file passed the backup leg",
            "missed": "the missing-journal-file case did not report its named diagnostic",
            "evidence": ["the backup carries no journal file"],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The driven ticks each phase runs — the pre-backup convergence, the
# settling window past the held write, and the post-restore
# reconvergence and continuation.
CONVERGE_TICKS = 4
SETTLE_TICKS = 2
HANDOVER_TICKS = 4
ACTOR = "ci-backup-restore"

# The held operator point the drill writes before backing up — the
# exercise program's writable run request, so the backup has retained
# operator state the restore must keep.
HELD_POINT = 240


def sha256(path):
    """The hex digest of one backup file — the integrity half of the
    backup record."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_tree_file(source, destination):
    """Copy one backup artifact, creating the destination directory."""
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    shutil.copyfile(source, destination)


def assemble_backup(args, manifest, duty_decl, standby_decl, files, backup):
    """Assemble the backup set beside the live deployment: the model
    and dynamics documents, the deployment manifest, and each declared
    controller's durability files — then record `backup.json`, the
    backup record naming the manifest pin, the document fingerprints,
    and every file's sha256. Returns the record."""
    os.makedirs(backup, exist_ok=True)
    for source, name in (
        (args.model, "model-plant.json"),
        (args.dynamics, "model-dynamics.json"),
        (args.manifest, "manifest.json"),
    ):
        copy_tree_file(source, os.path.join(backup, name))
    record = {
        "dcs_release": manifest["dcs_release"],
        "model": {
            "fingerprint": manifest["model"]["fingerprint"],
            "sha256": sha256(args.model),
        },
        "controllers": {},
        "files": {},
    }
    if manifest.get("dynamics", {}).get("fingerprint"):
        record["dynamics"] = {
            "fingerprint": manifest["dynamics"]["fingerprint"],
            "sha256": sha256(args.dynamics),
        }
    else:
        record["dynamics"] = {"sha256": sha256(args.dynamics)}
    for decl, peer_files in (
        (duty_decl, files[0]),
        (standby_decl, files[1]),
    ):
        name = decl["name"]
        entry = {}
        for field in ("state_file", "journal_file", "history_file"):
            declared = decl.get(field)
            live = peer_files.get(field)
            if not declared or live is None:
                continue
            if not os.path.exists(live):
                raise Abort(
                    f"{name}'s declared {field} {live} does not "
                    "exist — the running pair left no backup to take"
                )
            target = os.path.join(backup, name, os.path.basename(declared))
            copy_tree_file(live, target)
            entry[field] = os.path.relpath(target, backup)
            record["files"][entry[field]] = sha256(target)
        record["controllers"][name] = entry
    with open(os.path.join(backup, "backup.json"), "w") as handle:
        json.dump(record, handle, indent=2, sort_keys=True)
    return record


def audit_backup(backup, manifest, failures):
    """The completeness audit: the backup restores only as a whole —
    every named artifact present — so a partial set fails naming its
    missing member rather than restoring silently."""
    with open(os.path.join(backup, "backup.json")) as handle:
        record = json.load(handle)
    if record.get("dcs_release") != manifest["dcs_release"]:
        failures.append(
            f"the backup records dcs_release "
            f"{record.get('dcs_release')!r}, the deployment declares "
            f"{manifest['dcs_release']!r} — the manifest pin the "
            "restore rolls back by name"
        )
        raise Abort
    for name in ("model-plant.json", "model-dynamics.json", "manifest.json"):
        if not os.path.exists(os.path.join(backup, name)):
            failures.append(f"the backup carries no {name} document")
            raise Abort
    for relative, expected in record.get("files", {}).items():
        path = os.path.join(backup, relative)
        if not os.path.exists(path):
            continue
        if sha256(path) != expected:
            failures.append(
                f"the backup file {relative} fails its recorded "
                "sha256 — the set changed under its record"
            )
            raise Abort
    for controller in manifest.get("controllers", []):
        name = controller["name"]
        entry = record.get("controllers", {}).get(name)
        if entry is None:
            failures.append(
                f"the backup carries no record for controller {name}"
            )
            raise Abort
        for field in ("state_file", "journal_file"):
            if not controller.get(field):
                continue
            relative = (entry or {}).get(field)
            if relative is None or not os.path.exists(
                os.path.join(backup, relative)
            ):
                failures.append(
                    f"the backup carries no {field.replace('_', ' ')} "
                    f"for {name}"
                )
                raise Abort
    return record


def backup_pass(args, tamper):
    """The backup-and-restore run: converge, hold, back up, wipe,
    restore, resume, continue. Returns `(digest_entries, evidence,
    failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the backup leg "
            "has nothing to exercise"
        )
    manifest, duty_decl, standby_decl = declared
    scratch = tempfile.mkdtemp(prefix="dcs-backup-restore-")
    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared, None)
        duty_url, standby_url = rig.duty_url, rig.standby_url

        # Phase 1 — the declared pair converges and takes one held
        # operator write, so the backup has retained state, an
        # applied receipt, and journal transitions to keep.
        converged = rig.converge(failures)
        status, receipt = pair.request(
            f"{duty_url}/command",
            {
                "command": {
                    "write_value": {
                        "kind": "bool",
                        "point": HELD_POINT,
                        "value": {"bool": True},
                    }
                },
                "actor": ACTOR,
            },
        )
        if status != 200 or simulate.receipt_outcome(receipt) != "accepted":
            failures.append(
                f"the held write answered {status} {receipt}, "
                "expected an accepted receipt"
            )
            raise Abort
        for _ in range(SETTLE_TICKS):
            rig.tick(standby_url, duty_url, failures)
        receipts0 = pair.get(f"{duty_url}/receipts", "GET /receipts", failures)
        journal0 = pair.get(f"{duty_url}/journal", "GET /journal", failures)
        tick0 = pair.get(f"{duty_url}/snapshot", "GET /snapshot", failures)[
            "tick"
        ]
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "tick": tick0,
                "receipts": receipts0,
            }
        )

        # Phase 2 — the backup: the set assembled beside the live
        # deployment and recorded, then audited whole before any
        # restore runs. The doctored cases drop one named artifact
        # from the assembled set here.
        backup = os.path.join(scratch, "backup")
        record = assemble_backup(
            args,
            manifest,
            duty_decl,
            standby_decl,
            (rig.duty_files, rig.standby_files),
            backup,
        )
        if tamper == "missing-state-file":
            os.remove(
                os.path.join(backup, record["controllers"][duty_decl["name"]]["state_file"])
            )
        elif tamper == "missing-journal-file":
            os.remove(
                os.path.join(
                    backup,
                    record["controllers"][standby_decl["name"]]["journal_file"],
                )
            )
        record = audit_backup(backup, manifest, failures)
        persisted = {}
        for decl in (duty_decl, standby_decl):
            entry = record["controllers"][decl["name"]]
            with open(os.path.join(backup, entry["state_file"])) as handle:
                checkpoint = json.load(handle)
            persisted[decl["name"]] = checkpoint.get("tick")
        evidence["persisted"] = persisted
        evidence["backup_files"] = sorted(record["files"])
        digest_entries.append(
            {
                "phase": "backup",
                # The digest covers the record's stable content —
                # the pin, the document fingerprints, and the file
                # names — never the files' raw sha256: the state
                # file embeds the run's per-boot receipt nonces and
                # the journal file the restart consult's ephemeral
                # source, so their bytes legitimately differ pass
                # to pass while the restored run is identical.
                "dcs_release": record["dcs_release"],
                "model": record["model"],
                "dynamics": record.get("dynamics"),
                "controllers": sorted(record["controllers"]),
                "backup_files": sorted(record["files"]),
                "persisted": persisted,
            }
        )
        if tamper is not None:
            failures.append(
                f"the {tamper} backup passed the completeness audit — "
                "a partial set must never restore"
            )
            raise Abort

        # Phase 3 — the wipe and restore: both controllers stopped,
        # their volumes deleted, the backup set replayed onto fresh
        # volumes. The field server stands throughout — the volumes
        # are the deployment's durability mounts, not the field.
        pair.stop(rig.standby)
        rig.standby = None
        pair.stop(rig.duty)
        rig.duty = None
        fresh = os.path.join(scratch, "restored")
        restored_files = {}
        for decl in (duty_decl, standby_decl):
            entry = record["controllers"][decl["name"]]
            peer_files = {}
            for field in ("state_file", "journal_file", "history_file"):
                if field not in entry:
                    peer_files[field] = None
                    continue
                target = os.path.join(
                    fresh, decl["name"], os.path.basename(decl[field])
                )
                copy_tree_file(os.path.join(backup, entry[field]), target)
                peer_files[field] = target
            restored_files[decl["name"]] = peer_files
        rig.duty_files = restored_files[duty_decl["name"]]
        rig.standby_files = restored_files[standby_decl["name"]]

        # Phase 4 — the resume: the duty relaunches first onto its
        # restored files and must report its resume at its persisted
        # tick — never a silent cold start — serving the pre-wipe
        # receipt log verbatim and replaying the pre-wipe journal
        # behind the restart's run-boundary entry.
        rig.duty, rig.duty_url, preamble = pair.spawn_peer(
            args.controller,
            args.model,
            args.dt,
            rig.plant_addr,
            None,
            rig.duty_files,
            pair_token=pair.PAIR_TOKEN,
        )
        if rig.duty_url is None:
            raise Abort(
                f"the restored duty controller {duty_decl['name']} "
                f"exited at startup: "
                f"{'; '.join(preamble) or 'no diagnostic'}"
            )
        duty_url = rig.duty_url
        resumed = resume_tick(preamble, persisted[duty_decl["name"]], failures)
        evidence["duty_resumed"] = resumed
        snapshot = pair.get(f"{duty_url}/snapshot", "GET /snapshot", failures)
        if snapshot["tick"] != persisted[duty_decl["name"]]:
            failures.append(
                f"the restored duty reports tick {snapshot['tick']}, "
                f"the backup persisted "
                f"{persisted[duty_decl['name']]}"
            )
            raise Abort
        receipts1 = pair.get(f"{duty_url}/receipts", "GET /receipts", failures)
        if receipts1 != receipts0:
            failures.append(
                "the restored duty's receipt log diverges from the "
                "pre-wipe log — the run's audit did not survive the "
                "restore"
            )
            raise Abort
        journal1 = pair.get(f"{duty_url}/journal", "GET /journal", failures)
        if journal1[: len(journal0)] != journal0:
            failures.append(
                "the restored duty no longer answers the pre-wipe "
                "journal entries verbatim"
            )
            raise Abort
        rest = journal1[len(journal0):]
        boundary = {
            "seq": len(journal0) + 1,
            "tick": persisted[duty_decl["name"]],
            "event": {"run_boundary": {"run": 2}},
        }
        if not rest or rest[0] != boundary:
            failures.append(
                "the restored duty's served journal does not open "
                f"with the run-2 boundary entry {boundary}: {rest}"
            )
            raise Abort
        # A relaunched launched-active consults the incumbent before
        # its startup claim — both old peers are stopped, so the pull
        # is refused and the run journals the `restart_consult`
        # audit beside the boundary. Only that audit may follow the
        # boundary before the run's next scan.
        for entry in rest[1:]:
            if set(entry.get("event", {})) != {"restart_consult"}:
                failures.append(
                    "the restored duty's served journal carries a "
                    f"non-consult entry behind the boundary: {entry}"
                )
                raise Abort
        check_journal_file(
            rig.duty_files["journal_file"],
            persisted[duty_decl["name"]],
            duty_decl["name"],
            failures,
        )
        if failures:
            raise Abort

        # Phase 5 — the tracking peer rejoins onto its restored files
        # at its own persisted tick, reconverges, and the run
        # continues bumplessly with identical images and the receipt
        # log still one log.
        target = duty_url.removeprefix("http://")
        rig.standby, rig.standby_url, preamble = pair.spawn_peer(
            args.controller,
            args.model,
            args.dt,
            rig.plant_addr,
            target,
            rig.standby_files,
            pair_token=pair.PAIR_TOKEN,
        )
        if rig.standby_url is None:
            raise Abort(
                f"the restored standby controller "
                f"{standby_decl['name']} exited at startup: "
                f"{'; '.join(preamble) or 'no diagnostic'}"
            )
        standby_url = rig.standby_url
        resumed = resume_tick(
            preamble, persisted[standby_decl["name"]], failures
        )
        evidence["standby_resumed"] = resumed
        check_journal_file(
            rig.standby_files["journal_file"],
            persisted[standby_decl["name"]],
            standby_decl["name"],
            failures,
        )
        if failures:
            raise Abort
        reconverged = rig.converge(failures, count=HANDOVER_TICKS)
        receipts_duty = pair.get(
            f"{duty_url}/receipts", "GET /receipts", failures
        )
        receipts_standby = pair.get(
            f"{standby_url}/receipts", "GET /receipts", failures
        )
        if receipts_duty != receipts_standby or receipts_duty != receipts0:
            failures.append(
                "the peers' receipt logs diverged across the restore "
                "— the adopted audit is not the pre-wipe log"
            )
            raise Abort
        final_tick = reconverged["owner"]["tick"]
        final = pair.select_snapshot(reconverged["owner"])
        if final_tick <= tick0:
            failures.append(
                f"the restored run stands at tick {final_tick}, never "
                f"past the pre-wipe tick {tick0}"
            )
            raise Abort
        evidence["final_tick"] = final_tick
        digest_entries.append(
            {
                "phase": "resumed",
                "duty_resumed": evidence["duty_resumed"],
                "standby_resumed": evidence["standby_resumed"],
                "ticks": reconverged["ticks"],
                "receipts": receipts_duty,
                "final": final,
            }
        )
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        if rig is not None:
            rig.close()
        shutil.rmtree(scratch, ignore_errors=True)
    return digest_entries, evidence, failures


def resume_tick(preamble, persisted, failures):
    """The relaunch's resume assertion: the startup preamble must
    report the resume at the persisted tick — a missing report is the
    silent cold start the drill exists to catch."""
    line = next(
        (line for line in preamble if "resumed from state file" in line),
        None,
    )
    if line is None:
        failures.append(
            "the relaunched controller never reported a resume — its "
            f"cold start at tick 0 silently loses the persisted run "
            f"at tick {persisted}"
        )
        raise Abort
    match = re.search(r"at tick (\d+)", line)
    resumed = int(match.group(1)) if match else None
    if resumed != persisted:
        failures.append(
            f"the relaunch resumed at tick {resumed}, the backup "
            f"persisted {persisted}"
        )
        raise Abort
    return resumed


def check_journal_file(path, persisted, name, failures):
    """The durable file's contract across the restore: exactly the two
    run-boundary markers — run 1 cold at tick 0, run 2 at the
    persisted tick — and entry seqs continuing 1..n in file order
    across them."""
    records = pair.journal_records(path)
    boundaries = [record for kind, record in records if kind == "boundary"]
    if boundaries != [{"run": 1, "tick": 0}, {"run": 2, "tick": persisted}]:
        failures.append(
            f"{name}'s journal boundaries are {boundaries}, expected "
            f"run 1 at tick 0 and run 2 at the persisted tick "
            f"{persisted}"
        )
        return
    seqs = [record["seq"] for kind, record in records if kind == "entry"]
    if seqs != list(range(1, len(seqs) + 1)):
        failures.append(
            f"{name}'s journal seqs are not 1..n in order: {seqs}"
        )


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
        choices=["missing-state-file", "missing-journal-file"],
        help="drop the named artifact from the assembled backup — "
        "the pass must fail naming the missing artifact",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = backup_pass(args, args.tamper)
    except Abort as abort:
        for line in abort.args:
            eprint(f"backup-restore: {line}")
        return 1
    for failure in failures:
        eprint(f"backup-restore: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"backup-restore: the {args.tamper} case passed "
                "silently — the audit never noticed the missing artifact"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"backup-restore-digest {digest} — backed up "
        f"{len(evidence['backup_files'])} files at tick "
        f"{evidence['persisted']}, resumed duty at tick "
        f"{evidence['duty_resumed']} and standby at tick "
        f"{evidence['standby_resumed']}, run continued to tick "
        f"{evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
