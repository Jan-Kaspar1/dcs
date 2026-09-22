#!/usr/bin/env python3
"""The demote-boundary pending-command leg for the reference plant —
the consumer-side proof that a receipted command admitted on the
field owner and demoted past inside its pending window settles
exactly once on the deployed redundant pair (WW-ENG-003, WW-LCM-001 —
the settled demote-pending contract).

The pair leg (`ci/pair.py`) proves the declared pair switches
bumplessly; the refusal leg proves admission-time refusals; the
command-switch leg carries an `Accepted` invoke through the restore
promotion. None of them audit the pending window itself on the
deployed pair — the interval between an `accepted` receipt and the
scan boundary that would settle it, crossed by a demote. The settled
contract suspends the demoting peer's accepted-pending commands at
the write gate's close: each settles exactly once — carried into the
successor's adoption and applied at its first field-owning scan, or
`Rejected{superseded}` with the settle journaled — never
phantom-applied on the fenced quiesced image, never dropped
unaudited. The driven run makes the window deterministic:

- converges the declared standby to `tracking` through the pair
  leg's driven-tick loop;
- submits a receipted `write_value` on a declared writable internal
  `In` point — the same `p101-hand` target the force legs resolve,
  gated inert downstream while the pump stands in auto — through the
  active's `POST /command`, asserting the `accepted` receipt and
  driving no scan, so the admission stands pending when the window
  opens;
- issues the documented demote on the field owner and the promote on
  the converged standby inside that window — the promote boundary's
  final sync carrying the still-`Accepted` admission — asserting the
  promoted peer's receipt log holds it pending, not settled, not
  vanished;
- drives the demoted peer's first quiesced scan before the promoted
  peer's first field-owning scan — the decisive observation: the
  fenced image's journal must carry no `command_settled` for the
  admission, its receipt log must still hold it pending, and its
  served image must still read the baseline — a quiesced scan mints
  no `applied` the line never ordered;
- drives the promoted peer's first field-owning scan — the admission
  settling exactly once there — then tracking-first ticks until the
  demoted peer's pull adopts the settlement;
- audits both peers: each peer's served journal carries the
  admission's settle exactly once with the same single outcome —
  `applied` once per peer (the settler's boundary plus the adopted
  record) or `superseded` on the demoted peer alone — the peers'
  receipt logs identical with the admission's single outcome (or
  absent superseded), the served images agreeing (the applied value
  on both, or the baseline on neither), and each manifest-declared
  durable journal file carrying the same settle record in `seq`
  order;
- restores the pair's launch roles — the manifest-declared duty
  controller `active` and its standby `tracking` again.

Usage:

    demote_pending.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `demote-pending-digest <sha256>` line prints — the
check runs two passes and compares them
(`demote-pending-nondeterministic`). A contract violation reports
`demote-pending: …` lines on stderr and exits 1 — the check's
`demote-pending-failed`. `--tamper phantom-applied` doctors the
fenced-window expectation to the phantom settle a quiesced scan must
never journal — a correct peer fails it; `--tamper unaudited-drop`
doctors the audit to expect the admission vanished from every
receipt surface and journal — the silent-loss shape.
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

# The driven ticks each phase runs — the handover the demoted peer's
# pulls adopt the settle across, and the restore. The actor the leg's
# receipted submission declares.
SETTLE_TICKS = 4
RESTORE_TICKS = 4
ACTOR = "ci-demote-pending"

# The contract's terminal outcomes for a pending command crossed by a
# demote: carried into the successor's adoption and applied, or the
# named superseded rejection — never anything else.
OUTCOMES = ("applied", "superseded")


def submit(url, command, failures):
    """POST one receipted write to the active's `/command` and assert
    the `accepted` submission under the leg's actor — returns the
    receipt."""
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


def admission_hit(receipt, command):
    """Whether a served or journaled receipt is the leg's admission —
    the (command, actor) pair is unique to its submission."""
    return (
        receipt.get("command") == command
        and receipt.get("actor") == ACTOR
    )


def admission_receipts(receipts, command):
    """The admission's entries in a served `GET /receipts` list."""
    return [entry for entry in receipts if admission_hit(entry, command)]


def admission_settles(journal, command):
    """The `(seq, tick, outcome)` of a journal's `command_settled`
    entries answering the admission."""
    found = []
    for entry in journal:
        settled = entry.get("event", {}).get("command_settled")
        if settled is None:
            continue
        if admission_hit(settled.get("receipt", {}), command):
            found.append(
                (
                    entry["seq"],
                    entry["tick"],
                    simulate.receipt_outcome(settled["receipt"]),
                )
            )
    return found


def demote_pending_pass(args, tamper):
    """The demote-pending run: converge, admit, demote inside the
    pending window, audit the fenced image, settle, audit both peers,
    restore. Returns `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the demote-pending "
            "leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    with open(args.model) as handle:
        model = json.load(handle)
    point = force_carryover.force_target(model)
    if point is None:
        raise Abort(
            f"the emitted model declares no writable In point at "
            f"{force_carryover.FORCE_SIGNAL} — the demote-pending leg "
            "has nothing to exercise"
        )

    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared, None)
        duty_url, standby_url = rig.duty_url, rig.standby_url

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

        # Phase 2 — the admission: a receipted write on the field
        # owner inside the pending window — accepted, then never
        # scanned, so it stands pending when the demote lands. The
        # baseline must differ from the written value or the settle's
        # image evidence proves nothing.
        baseline = simulate.snapshot_point(owner, point)
        if baseline is None or "bool" not in baseline:
            failures.append(
                f"the admission target point {point} serves {baseline} "
                "— a bool baseline the leg can flip is required"
            )
            raise Abort
        value = {"bool": not baseline["bool"]}
        command = takeover.write_value(point, value["bool"])
        receipt = submit(duty_url, command, failures)
        if simulate.receipt_outcome(receipt) != "accepted":
            failures.append(
                f"the admission answered "
                f"{simulate.receipt_outcome(receipt)}, expected the "
                "accepted receipt the pending window needs"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "admit",
                "point": point,
                "baseline": baseline,
                "command": command,
                "receipt": receipt,
            }
        )

        # Phase 3 — the boundary: demote the field owner and promote
        # the converged standby inside the pending window. The
        # promote's final sync carries the still-`Accepted` admission
        # into the successor's log — asserted held pending, never
        # vanished, never pre-settled.
        demote = rig.demote(duty_url, failures)
        promote = rig.promote(standby_url, failures)
        evidence["demoted_at"] = demote["tick"]
        promoted_receipts = pair.get(
            f"{standby_url}/receipts", "GET /receipts", failures
        )
        carried = admission_receipts(promoted_receipts, command)
        if len(carried) != 1 or simulate.receipt_outcome(carried[0]) != "accepted":
            failures.append(
                f"the promoted peer holds {carried} for the admission "
                "after the boundary — expected the single carried "
                "accepted receipt: the pending command was lost or "
                "pre-settled at the switch"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "boundary",
                "demote": demote,
                "promote": promote,
                "carried": carried,
            }
        )

        # Phase 4 — the fenced window's decisive scan: the demoted
        # peer's first quiesced scan, driven before the promoted
        # peer's first field-owning scan. A quiesced scan applies no
        # commands — the suspended admission must still stand pending
        # on the fenced image: no `command_settled` journaled, the
        # receipt still `accepted`, the point still reading baseline.
        # Under `phantom-applied` the leg doctors its own expectation
        # to the defect shape — the phantom applied settle a fenced
        # image must never journal.
        demoted_scan = pair.scan(duty_url, failures)
        demoted_journal = pair.get(
            f"{duty_url}/journal", "GET /journal", failures
        )
        demoted_receipts = pair.get(
            f"{duty_url}/receipts", "GET /receipts", failures
        )
        fenced_settles = admission_settles(demoted_journal, command)
        if tamper == "phantom-applied":
            if len(fenced_settles) != 1 or fenced_settles[0][2] != "applied":
                failures.append(
                    f"the demoted peer's quiesced scan journaled "
                    f"{fenced_settles} for the admission — expected "
                    "the phantom applied settle the doctored case "
                    "requires, a command_settled{applied} the fenced "
                    "image must never produce"
                )
        elif fenced_settles:
            failures.append(
                f"the demoted peer's fenced image journaled "
                f"{fenced_settles} for the admission — a quiesced "
                "scan must mint no command_settled for a command the "
                "line still holds pending"
            )
        held = admission_receipts(demoted_receipts, command)
        if len(held) != 1 or simulate.receipt_outcome(held[0]) != "accepted":
            failures.append(
                f"the demoted peer's receipt log holds {held} for the "
                "admission inside the fenced window — expected the "
                "suspended accepted receipt: the pending entry "
                "vanished or settled unaudited"
            )
        fenced_value = simulate.snapshot_point(demoted_scan, point)
        if fenced_value != baseline:
            failures.append(
                f"the demoted peer's fenced image reads {fenced_value} "
                f"for point {point}, expected the baseline {baseline} "
                "— a phantom application the journal never settled"
            )
        demoted_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        if demoted_role.get("role") != "standby":
            failures.append(
                f"the demoted peer reports {demoted_role.get('role')!r} "
                "after its first quiesced scan, expected standby"
            )
        if failures:
            raise Abort
        evidence["fenced_at"] = demoted_scan["tick"]
        digest_entries.append(
            {
                "phase": "fenced-window",
                "tick": demoted_scan["tick"],
                "settles": fenced_settles,
                "held": held,
                "point": fenced_value,
                "role": demoted_role,
            }
        )

        # Phase 5 — the settle: the promoted peer's first field-owning
        # scan settles the carried admission exactly once, then the
        # driven ticks converge the demoted peer's adopted record.
        promoted_scan = pair.scan(standby_url, failures)
        settle_ticks = [promoted_scan["tick"]]
        for _ in range(SETTLE_TICKS):
            _tracked, owner = rig.tick(duty_url, standby_url, failures)
            settle_ticks.append(owner["tick"])
        promoted_role = pair.get(
            f"{standby_url}/role", "GET /role", failures
        )
        demoted_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        if promoted_role.get("role") != "active":
            failures.append(
                f"the promoted peer reports "
                f"{promoted_role.get('role')!r}, expected active"
            )
        sync = demoted_role.get("sync")
        if demoted_role.get("role") != "standby" or not (
            isinstance(sync, dict) and "tracking" in sync
        ):
            failures.append(
                "the demoted peer never reconverged — GET /role "
                f"answers {demoted_role}"
            )
        if failures:
            raise Abort
        evidence["settled_at"] = settle_ticks[-1]
        digest_entries.append(
            {
                "phase": "settle",
                "ticks": settle_ticks,
                "promoted_role": promoted_role,
                "demoted_role": demoted_role,
            }
        )

        # Phase 6 — the audit: both peers' served journals and receipt
        # logs plus each manifest-declared durable journal file. The
        # admission settles exactly once: `applied` once per peer (the
        # settler's boundary and the adopted record) or `superseded`
        # on the demoted peer alone — never two terminal outcomes,
        # never a settle the other peer's log contradicts, never a
        # vanished pending entry. Under `unaudited-drop` the leg
        # doctors its expectation to the silent-loss shape — the
        # admission vanished from every receipt surface and journal.
        journals = {
            name: pair.get(f"{url}/journal", "GET /journal", failures)
            for name, url in (
                (duty_decl["name"], duty_url),
                (standby_decl["name"], standby_url),
            )
        }
        receipts = {
            name: pair.get(f"{url}/receipts", "GET /receipts", failures)
            for name, url in (
                (duty_decl["name"], duty_url),
                (standby_decl["name"], standby_url),
            )
        }
        settles = {
            name: admission_settles(journal, command)
            for name, journal in journals.items()
        }
        logged = {
            name: admission_receipts(log, command)
            for name, log in receipts.items()
        }
        outcomes = {
            outcome
            for entries in settles.values()
            for _seq, _tick, outcome in entries
        }
        if tamper == "unaudited-drop":
            vanished = not outcomes and not any(logged.values())
            if not vanished:
                failures.append(
                    f"the admission still evidences settles={settles} "
                    f"logged={logged} — expected the unaudited drop "
                    "the doctored case requires, the pending entry "
                    "gone from every receipt surface and journal"
                )
        else:
            if len(outcomes) != 1:
                failures.append(
                    f"the admission journaled {sorted(outcomes)} — one "
                    "pending command must settle exactly one terminal "
                    "outcome across both peers"
                )
            elif next(iter(outcomes)) not in OUTCOMES:
                failures.append(
                    f"the admission settled "
                    f"{next(iter(outcomes))} — neither the carried "
                    "applied nor the named superseded rejection"
                )
            else:
                outcome = next(iter(outcomes))
                for name in (duty_decl["name"], standby_decl["name"]):
                    want = 1 if name == duty_decl["name"] or outcome == "applied" else 0
                    if len(settles[name]) != want:
                        failures.append(
                            f"{name}'s served journal carries "
                            f"{len(settles[name])} command_settled "
                            f"records for the admission — the contract "
                            f"settles exactly {want} there"
                        )
                if receipts[duty_decl["name"]] != receipts[standby_decl["name"]]:
                    failures.append(
                        "the peers' receipt logs diverged across the "
                        "demote boundary — the adopted audit is not "
                        "one log"
                    )
                for name in (duty_decl["name"], standby_decl["name"]):
                    if len(logged[name]) > 1:
                        failures.append(
                            f"{name}'s adopted log carries "
                            f"{len(logged[name])} receipts for the "
                            "admission"
                        )
                    for entry in logged[name]:
                        if simulate.receipt_outcome(entry) != outcome:
                            failures.append(
                                f"{name}'s adopted log carries "
                                f"{simulate.receipt_outcome(entry)} for "
                                "the admission where the journal "
                                f"settled {outcome}"
                            )
                    if outcome == "applied" and not logged[name]:
                        failures.append(
                            f"{name}'s adopted log lost the admission "
                            "the journal settled applied"
                        )
                    image = simulate.snapshot_point(
                        pair.get(
                            f"{duty_url if name == duty_decl['name'] else standby_url}"
                            "/snapshot",
                            "GET /snapshot",
                            failures,
                        ),
                        point,
                    )
                    want_image = value if outcome == "applied" else baseline
                    if image != want_image:
                        failures.append(
                            f"{name}'s image reads {image} for point "
                            f"{point}, expected {want_image} — the "
                            "settle's application evidence is missing "
                            "or phantom"
                        )
        if failures:
            raise Abort
        outcome = next(iter(outcomes)) if len(outcomes) == 1 else None
        evidence["outcome"] = outcome
        digest_entries.append(
            {
                "phase": "audit",
                "outcome": outcome,
                "settles": settles,
                "logged": logged,
            }
        )

        # Phase 7 — the durable record: each peer's declared journal
        # file must carry the same settle the served journal did —
        # the demoted peer's file the journaled audit of the boundary,
        # never a phantom applied on the fenced image.
        files = {
            duty_decl["name"]: rig.duty_files,
            standby_decl["name"]: rig.standby_files,
        }
        persisted = {}
        for name in (duty_decl["name"], standby_decl["name"]):
            journal_path = files[name].get("journal_file")
            if journal_path is None or not os.path.exists(journal_path):
                failures.append(
                    f"{name}'s declared journal file {journal_path} "
                    "does not exist — the --journal-file flag was not "
                    "honored"
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
            entries = [record for kind, record in records if kind == "entry"]
            seqs = [entry["seq"] for entry in entries]
            if seqs != list(range(1, len(seqs) + 1)):
                failures.append(
                    f"{name}'s journal seqs are not 1..n in order: {seqs}"
                )
            file_settles = admission_settles(entries, command)
            if file_settles != settles[name]:
                failures.append(
                    f"{name}'s durable journal carries {file_settles} "
                    "for the admission where its served journal "
                    f"carries {settles[name]} — the durable audit is "
                    "not the served audit"
                )
            persisted[name] = file_settles
        if failures:
            raise Abort
        evidence["persisted"] = sum(
            len(entries) for entries in persisted.values()
        )
        digest_entries.append({"phase": "record", "persisted": persisted})

        # Phase 8 — the roles restore: demote the new owner, promote
        # the reconverged peer, and drive the pair back to the
        # manifest's declared arrangement — the duty controller
        # `active`, its standby `tracking`.
        demote = rig.demote(standby_url, failures, "the new field owner")
        promote = rig.promote(duty_url, failures, "the reconverged peer")
        restore_ticks = []
        for _ in range(RESTORE_TICKS):
            _tracked, owner = rig.tick(standby_url, duty_url, failures)
            restore_ticks.append(owner["tick"])
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        standby_role = pair.get(
            f"{standby_url}/role", "GET /role", failures
        )
        if duty_role.get("role") != "active":
            failures.append(
                f"the restored field owner reports "
                f"{duty_role.get('role')!r}, expected active — the "
                "pair was not left in its declared roles"
            )
        sync = standby_role.get("sync")
        if standby_role.get("role") != "standby" or not (
            isinstance(sync, dict) and "tracking" in sync
        ):
            failures.append(
                "the restored standby never reconverged — GET /role "
                f"answers {standby_role}"
            )
        if failures:
            raise Abort
        evidence["restored_at"] = restore_ticks[-1]
        digest_entries.append(
            {
                "phase": "restore",
                "demote": demote,
                "promote": promote,
                "ticks": restore_ticks,
                "duty_role": duty_role,
                "standby_role": standby_role,
            }
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
        choices=["phantom-applied", "unaudited-drop"],
        help="doctor the leg's expectations to the defect shape — the "
        "pass must fail naming the evidence the contract forbids",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = demote_pending_pass(
            args, args.tamper
        )
    except Abort as abort:
        for line in abort.args:
            eprint(f"demote-pending: {line}")
        return 1
    for failure in failures:
        eprint(f"demote-pending: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"demote-pending: the {args.tamper} case passed "
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
        f"demote-pending-digest {digest} — tracking by tick "
        f"{evidence['converged']}, demoted inside the pending window "
        f"at tick {evidence['demoted_at']}, fenced image audited at "
        f"tick {evidence['fenced_at']}, settled {evidence['outcome']} "
        f"by tick {evidence['settled_at']}, {evidence['persisted']} "
        f"persisted settle records, roles restored at tick "
        f"{evidence['restored_at']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
