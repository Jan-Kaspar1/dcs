#!/usr/bin/env python3
"""The suspended-receipt alias-audit leg for the reference plant —
the consumer-side proof that a receipted command suspended at a
fenced demote keeps its audit identity when the adopted receipt
window carries an identical *command* at its absolute index
(WW-ENG-003, WW-FND-004 — the #1080 suspended-alias contract the
rig's suspended-alias-audit leg pins in-workspace, mirrored at the
customer boundary).

The demote-pending leg (`ci/legs/demote_pending.py`) proves a pending
command crossed by a documented switch settles exactly once; the
claim-fencing leg (`ci/claim_fencing.py`) proves a preempt-window
admission settles `superseded` on both peers. Neither stages the
collision the finding drove: two *different admissions* minting the
same command at the same absolute index on opposite sides of the
switch — a suspended receipt and an identical (point, value) write
the promoted peer mints on the stale adopted window it carries past
the stopped holder's final-sync fetch. The adopted entry is the
suspended submission's carry only when it is the same submission —
the verbatim receipt or its terminal settlement, `actor` and
`reason` included; an identical command a different admission wrote
is a different command at the index, and the suspended entry must
resolve `Rejected{superseded}` journaled exactly once — never
absorbed as the promoted peer's carry, never vanished unaudited.

The driven pair makes the collision deterministic without the rig's
third controller: the tracking peer's pulls live only inside its own
driven `POST /scan`, so the leg freezes the adopted window simply by
not scanning it. The run:

- converges the declared pair to `tracking` through the pair leg's
  driven-tick loop and reads both peers' submission high-waters —
  the absolute index the next `POST /command` mints, which must
  agree;
- preempts the field's writer claim with a rogue attachment's
  unconditional `claim_writer` — the fenced demote the suspension
  rides — then submits the suspended admission on the still-active
  field owner: an identical point and value to the promoted peer's
  coming command under a distinct `(actor, reason)` identity,
  receipted `accepted` at the collision index;
- drives the superseded owner's detection scans until it settles
  `standby`, asserting the admission still serves `accepted` at its
  index — the suspension the successor's adopted window will
  collide with;
- stops the holder so the promoted peer's promote-boundary
  final-sync fetch has nothing to pull — its adopted window
  predating the suspended admission, its submission high-water
  still minting at the collision index — promotes the tracking
  peer, and submits the identical command there: same point, same
  value, a different admission identity, minted at the suspended
  admission's own index;
- rejoins the holder as a standby on its declared listen address
  wired at the promoted peer — the consumer pair's rejoin-as-standby
  boundary, its suspended receipt re-queued from the state file —
  and drives the covering adoption: the adopted window's identical
  entry meets the holder's suspended receipt at the collision
  index;
- audits both serving monitors and both declared durable journals:
  the suspended admission is adjudicated `rejected{superseded}`
  journaled exactly once on its holder — never silently dropped,
  never absorbed under the identical admission's identity, never
  journaled on the promoted peer that never held it — while the
  identical admission settles `applied` exactly once per peer on
  its own receipt index;
- restores the pair's launch roles — the manifest-declared duty
  controller `active`, its standby `tracking`.

The contract postdates the pinned v0.3.0 release: where the launched
tooling predates it the run's own evidence is the pre-contract shape
— a checkpoint carrying no receipt window or admission counters, a
receipt dropping its declared `actor`/`reason`, a refused preemption,
a demote settling rather than suspending the admission, or the
covering adoption leaving the suspended admission absorbed —
`vanished` or still suspended — without its named superseded
boundary — and the leg reports
`suspended-alias-audit-digest inconclusive` rather than asserting
until the manifest repins a release carrying the contract.

Usage:

    suspended_alias_audit.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `suspended-alias-audit-digest <sha256>` line prints —
the check runs two passes and compares them
(`suspended-alias-audit-nondeterministic`). A contract violation
reports `suspended-alias-audit: …` lines on stderr and exits 1 — the
check's `suspended-alias-audit-failed`. `--tamper vanished-admission`
doctors the leg's own expectation to the defect shape — asserting
the suspended admission may vanish unaudited — so the leg proves its
audit fires on the honest adjudicated record rather than passing an
unexercised contract.
"""

import argparse
import hashlib
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import force_carryover
import pair
import simulate
import takeover


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored case: a leg asserting the suspended admission may
# vanish unaudited must surface the named diagnostic on the honest
# adjudicated record — never a silently unexercised contract.
LEG = {
    "order": 370,
    "title": "the suspended-receipt alias-audit leg",
    "passes": "suspended-alias-audit",
    "tampers": [
        {
            "name": "vanished-admission",
            "passed": "a vanished-admission case passed the suspended-alias-audit leg",
            "missed": "the vanished-admission case did not report its named diagnostic",
            "evidence": ["the doctored expectation wanted the suspended admission vanished"],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The pinned release predates — or never covers — the contract
    the leg exercises: the run classifies inconclusive, never a
    product failure."""


# The claim token the rogue attachment preempts with — a small fixed
# token that cannot collide with a controller's per-process minted
# token, the same token the claim-fencing leg's rogue probe declares.
CLAIM_ROGUE = 0xF00E

# The driven-scan bounds each transition gets: the fenced demote's
# detection scans, the promoted peer's settle, the rejoined holder's
# covering adoption, and the launch-role restore.
WATCH_SCANS = 6
RESOLVE_SCANS = 8
IDENTICAL_SCANS = 2
RESTORE_TICKS = 6

# The admissions' declared identities — unique in both peers' receipt
# logs, the whole-record identity the carry test compares.
ACTOR_SUSPENDED = "ci-alias-susp"
REASON_SUSPENDED = "suspended-alias-audit"
ACTOR_IDENTICAL = "ci-alias-ident"
REASON_IDENTICAL = "suspended-alias-ident"


def admission_hit(receipt, command, actor):
    """Whether a served or journaled receipt is the named admission —
    the (command, actor) pair is unique to its submission."""
    return (
        isinstance(receipt, dict)
        and receipt.get("command") == command
        and receipt.get("actor") == actor
    )


def admission_settles(entries, command, actor):
    """The `(seq, tick, outcome)` of a journal entry list's
    `command_settled` records answering the admission — served
    `GET /journal` entries and durable journal-file records alike."""
    found = []
    for entry in entries:
        settled = entry.get("event", {}).get("command_settled")
        if settled is None:
            continue
        if admission_hit(settled.get("receipt", {}), command, actor):
            found.append(
                (
                    entry["seq"],
                    entry["tick"],
                    simulate.receipt_outcome(settled["receipt"]),
                )
            )
    return found


def journal_entries(path):
    """A `--journal-file`'s entry records in file order — the durable
    half of the audit."""
    return [
        record
        for kind, record in pair.journal_records(path)
        if kind == "entry"
    ]


def receipt_window(url, what, failures):
    """The retained receipt tail and the absolute submission index of
    its first entry — the (log, high-water) pair the collision audit
    correlates by, from the checkpoint's carried window and admission
    counters rather than the bare `GET /receipts` tail whose place in
    the sequence is unsaid."""
    checkpoint = pair.get(f"{url}/checkpoint", what, failures)
    receipts = checkpoint.get("receipts")
    attempts = (checkpoint.get("command_admission") or {}).get("attempts")
    if not isinstance(receipts, list) or not isinstance(attempts, int):
        raise Inconclusive(
            "the served checkpoint carries no receipt window or "
            "admission counters — the index correlation the contract "
            "stands on; the pinned release predates the "
            "suspended-alias-audit contract"
        )
    return receipts, max(0, attempts - len(receipts))


def next_receipt_index(url, what, failures):
    """The absolute submission index the peer's next `POST /command`
    mints — the window's high-water `base + len(receipts)`."""
    receipts, base = receipt_window(url, what, failures)
    return base + len(receipts)


def index_receipt(url, index, what, failures):
    """The served receipt at absolute submission `index`, or None
    while the retained window does not cover it — the suspended
    admission's still-standing proof and the promoted peer's
    own-index proof for the identical admission."""
    receipts, base = receipt_window(url, what, failures)
    position = index - base
    if 0 <= position < len(receipts):
        return receipts[position]
    return None


def tracking(report):
    """Whether a served RoleReport carries `standby` under the
    `tracking` sync state — the promotable posture the pair's launch
    roles declare."""
    sync = report.get("sync") if isinstance(report, dict) else None
    return report.get("role") == "standby" and (
        isinstance(sync, dict) and "tracking" in sync
    )


def suspended_verdict(settles, logged, command):
    """The suspended admission's audit verdict once the covering
    adoption landed: `settles` is the holder's `command_settled`
    matches for the admission and `logged` its served receipt-log
    matches. Adjudicated-by-name is the contract's only settled
    shape — a second settle or another outcome diverges, a vanished
    or still-suspended admission is the pre-contract absence the
    leg classifies inconclusive. Returns the digest verdict."""
    if len(settles) > 1:
        return "diverged"
    if settles:
        return (
            "adjudicated" if settles[0][2] == "superseded" else "diverged"
        )
    if any(
        admission_hit(receipt, command, ACTOR_SUSPENDED)
        and simulate.receipt_outcome(receipt) == "accepted"
        for receipt in logged
    ):
        return "suspended"
    return "vanished"


def suspended_alias_pass(args, tamper):
    """The suspended-alias run: converge, preempt, suspend, fence the
    demote, freeze the adopted window behind the stopped holder,
    mint the identical command at the collision index, rejoin and
    cover, audit both monitors and both durable journals, restore.
    Returns `(digest_entries, evidence, failures)`; raises
    `Inconclusive` where the pinned release predates the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "suspended-alias-audit leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    with open(args.model) as handle:
        model = json.load(handle)
    point = force_carryover.force_target(model)
    if point is None:
        raise Abort(
            f"the emitted model declares no writable In point at "
            f"{force_carryover.FORCE_SIGNAL} — the suspended-alias "
            "leg has nothing to suspend"
        )

    digest_entries, evidence, failures = [], {}, []
    rig = None
    probe_io = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url

        # Phase 1 — convergence: the pair leg's driven-tick loop, the
        # tracking peer scanned first so each pull applies the owner's
        # latest checkpoint and the peers rest at the same tick with
        # identical images.
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

        # The contract surface: both peers' checkpoints must carry
        # the receipt window and admission counters the index
        # correlation reads, and the pair's declared persistence —
        # the holder's state file the suspended receipt re-queues
        # from, both journal files the durable half of the audit —
        # must stand. A surface absent is the release predating the
        # contract, never a violation of it.
        for name, url in (
            (duty_decl["name"], duty_url),
            (standby_decl["name"], standby_url),
        ):
            receipt_window(url, f"GET /checkpoint on {name}", failures)
        if (
            rig.duty_files.get("state_file") is None
            or rig.duty_files.get("journal_file") is None
            or rig.standby_files.get("journal_file") is None
        ):
            raise Inconclusive(
                "the manifest's pair declares no "
                "state_file/journal_file persistence — the durable "
                "half of the audit and the holder's suspended-receipt "
                "re-queue are absent"
            )
        baseline = simulate.snapshot_point(owner, point)
        if baseline is None or "bool" not in baseline:
            raise Abort(
                f"the suspension target point {point} serves "
                f"{baseline} — a bool baseline the leg can flip is "
                "required"
            )
        value = not baseline["bool"]
        command = takeover.write_value(point, value)

        # The collision index: the absolute submission index the
        # promoted peer's stale window will mint at — the field
        # owner's next index must agree, else the converged line
        # never stood and the identical command cannot land on the
        # suspended admission's index.
        alias_index = next_receipt_index(
            duty_url, "GET /checkpoint", failures
        )
        peer_index = next_receipt_index(
            standby_url, "GET /checkpoint", failures
        )
        evidence["indices"] = {"duty": alias_index, "standby": peer_index}
        if alias_index != peer_index:
            failures.append(
                "the converged peers disagree on the submission "
                f"high-water — the tracking peer mints at {peer_index} "
                f"where the owner mints at {alias_index} — the "
                "identical command cannot alias the suspended "
                "admission"
            )
            raise Abort

        # Phase 2 — the preemption the suspension window needs: a
        # rogue attachment takes the field claim under the standing
        # owner, so the still-reporting-active gate admits the
        # suspended submission and the detection scan demotes the
        # holder in place — the fenced demote the issue names. The
        # rig's own client stays read-only, so the claim belongs to
        # this one connection; the promote boundary's unconditional
        # claim preempts it below.
        probe_io = simulate.PlantClient(rig.plant_addr)
        preempt = probe_io.request(
            {"op": "claim_writer", "owner": CLAIM_ROGUE}
        )
        evidence["preempt"] = preempt
        if preempt.get("result") != "done":
            raise Inconclusive(
                f"the rogue claim answered {preempt} — the consumer "
                "harness admits no unconditional preemption lever, "
                "or the pinned plant predates it"
            )

        # Phase 3 — the suspended admission: identical point and
        # value to the promoted peer's coming command under a
        # distinct (actor, reason) identity, admitted on the
        # still-reporting-active owner at the collision index.
        status, suspended = pair.request(
            f"{duty_url}/command",
            {
                "command": command,
                "actor": ACTOR_SUSPENDED,
                "reason": REASON_SUSPENDED,
            },
        )
        evidence["suspended_submission"] = {
            "status": status,
            "receipt": suspended,
        }
        if status != 200 or simulate.receipt_outcome(suspended) != "accepted":
            failures.append(
                f"the suspended-window submission answered {status} "
                f"{suspended}, expected an accepted receipt at index "
                f"{alias_index}"
            )
            raise Abort
        if (
            suspended.get("actor") != ACTOR_SUSPENDED
            or suspended.get("reason") != REASON_SUSPENDED
        ):
            raise Inconclusive(
                "the served receipt drops the declared actor/reason "
                "— the submission-record identity the carry test "
                "compares; the pinned release predates the "
                "suspended-alias-audit contract"
            )
        digest_entries.append(
            {
                "phase": "suspend",
                "index": alias_index,
                "point": point,
                "receipt": suspended,
            }
        )

        # Phase 4 — the demote-in-place watch: the superseded owner's
        # detection scans walk demoting to standby with its monitor
        # answering every poll, the receipt still `accepted` at the
        # collision index — the suspension the successor's adopted
        # window collides with. A boundary that settled the admission
        # instead of suspending it is the pre-contract shape.
        roles = []
        settled = None
        for _ in range(WATCH_SCANS):
            pair.scan(duty_url, failures)
            report = pair.get(f"{duty_url}/role", "GET /role", failures)
            roles.append(report.get("role"))
            if report.get("role") == "standby":
                settled = report
                break
        evidence["demote_watch"] = roles
        if settled is None:
            failures.append(
                "the superseded owner never demoted — its reported "
                f"role stayed {roles}"
            )
            raise Abort
        standing = index_receipt(
            duty_url, alias_index, "GET /checkpoint", failures
        )
        evidence["suspended_standing"] = standing
        if not admission_hit(standing, command, ACTOR_SUSPENDED):
            raise Inconclusive(
                f"the suspended admission's served receipt reads "
                f"{standing} at index {alias_index} after the demote "
                "— the admission never stood suspended; the pinned "
                "release predates the suspended-entry contract"
            )
        if simulate.receipt_outcome(standing) != "accepted":
            raise Inconclusive(
                f"the demote settled the admission "
                f"{simulate.receipt_outcome(standing)} instead of "
                "suspending it — the pinned release predates the "
                "suspended-entry contract"
            )
        digest_entries.append(
            {
                "phase": "demote",
                "watch": roles,
                "standing": "accepted",
            }
        )

        # Phase 5 — the frozen adopted window: stop the holder so the
        # promote boundary's final-sync fetch has nothing live to
        # pull — the promoted peer's window predates the suspended
        # admission and its high-water still mints at the collision
        # index.
        duty_listen = duty_url.removeprefix("http://")
        pair.stop(rig.duty)
        status, promote = pair.request(f"{standby_url}/promote", {})
        evidence["promote"] = {"status": status, "body": promote}
        if status != 200:
            failures.append(
                f"POST /promote on the tracking peer answered "
                f"{status} {promote}"
            )
            raise Abort
        owned = None
        for _ in range(WATCH_SCANS):
            pair.scan(standby_url, failures)
            report = pair.get(
                f"{standby_url}/role", "GET /role", failures
            )
            if report.get("role") == "active":
                owned = report
                break
        if owned is None:
            failures.append(
                "the promoted peer never settled into the active "
                f"role — GET /role answers {report}"
            )
            raise Abort
        mint_index = next_receipt_index(
            standby_url, "GET /checkpoint", failures
        )
        evidence["mint_index"] = mint_index
        if mint_index != alias_index:
            failures.append(
                "the promoted peer's submission high-water moved to "
                f"{mint_index} — the identical command cannot mint "
                f"at the suspended admission's index {alias_index} "
                "— its final-sync pull carried the window"
            )
            raise Abort

        # Phase 6 — the identical admission: same point, same value,
        # a distinct admission identity, minted at the suspended
        # admission's absolute index — the adopted-window entry a
        # command-only carry test would alias.
        status, identical = pair.request(
            f"{standby_url}/command",
            {
                "command": command,
                "actor": ACTOR_IDENTICAL,
                "reason": REASON_IDENTICAL,
            },
        )
        evidence["identical_submission"] = {
            "status": status,
            "receipt": identical,
        }
        if status != 200 or simulate.receipt_outcome(identical) != "accepted":
            failures.append(
                f"the identical submission on the promoted peer "
                f"answered {status} {identical}"
            )
            raise Abort
        if (
            identical.get("actor") != ACTOR_IDENTICAL
            or identical.get("reason") != REASON_IDENTICAL
        ):
            raise Inconclusive(
                "the promoted peer's served receipt drops the "
                "declared actor/reason — the submission-record "
                "identity the carry test compares; the pinned "
                "release predates the suspended-alias-audit contract"
            )
        for _ in range(IDENTICAL_SCANS):
            pair.scan(standby_url, failures)
        digest_entries.append(
            {
                "phase": "identical",
                "index": alias_index,
                "receipt": identical,
            }
        )

        # Phase 7 — the covering path: the holder rejoins as a
        # standby on its declared listen address wired at the
        # promoted peer — the released controller's own
        # rejoin-as-standby boundary — its suspended receipt
        # re-queued from the state file, its tracking pulls adopting
        # the promoted line's record. The adjudication the contract
        # owes lands inside those pulls.
        rig.duty, url, preamble = pair.spawn_peer(
            args.controller,
            args.model,
            args.dt,
            rig.plant_addr,
            standby_url.removeprefix("http://"),
            rig.duty_files,
            listen=duty_listen,
            pair_token=pair.PAIR_TOKEN,
        )
        rig.duty_url = url
        if url is None:
            failures.append(
                "the suspended admission's holder rejoined as a "
                f"standby exited at startup: "
                f"{'; '.join(preamble[-2:]) or 'no diagnostic'}"
            )
            raise Abort
        resolved = None
        for _ in range(RESOLVE_SCANS):
            pair.scan(duty_url, failures)
            pair.scan(standby_url, failures)
            duty_role = pair.get(
                f"{duty_url}/role", "GET /role", failures
            )
            peer_role = pair.get(
                f"{standby_url}/role", "GET /role", failures
            )
            if tracking(duty_role) and peer_role.get("role") == "active":
                resolved = (duty_role, peer_role)
                break
        evidence["resolved"] = resolved
        if resolved is None:
            failures.append(
                "the rejoined holder never converged tracking behind "
                "the promoted owner — the covering adoption the "
                "suspended admission waits on never landed: "
                f"{duty_role} / {peer_role}"
            )
            raise Abort

        # Phase 8 — the audit. Both serving monitors' journals since
        # the run's floors and both declared durable journal files:
        # the suspended admission adjudicated `superseded` journaled
        # exactly once on its holder — never vanished, never a stray
        # settle on the peer that never held it — and the identical
        # admission settled `applied` exactly once per peer on its
        # own receipt index.
        journals = {
            name: pair.get(f"{url}/journal", "GET /journal", failures)
            for name, url in (
                (duty_decl["name"], duty_url),
                (standby_decl["name"], standby_url),
            )
        }
        logged = {
            name: pair.get(f"{url}/receipts", "GET /receipts", failures)
            for name, url in (
                (duty_decl["name"], duty_url),
                (standby_decl["name"], standby_url),
            )
        }
        settles = {
            name: admission_settles(
                journal, command, ACTOR_SUSPENDED
            )
            for name, journal in journals.items()
        }
        identical_settles = {
            name: admission_settles(
                journal, command, ACTOR_IDENTICAL
            )
            for name, journal in journals.items()
        }
        verdict = suspended_verdict(
            settles[duty_decl["name"]],
            logged[duty_decl["name"]],
            command,
        )
        evidence["verdict"] = verdict
        if verdict == "vanished":
            raise Inconclusive(
                "the suspended admission vanished unaudited — no "
                "journaled settle on its holder and no attributable "
                "receipt in its served log: the adopted window's "
                "identical entry aliased it as carried, the "
                "pre-contract shape; the pinned release predates the "
                "suspended-alias-audit contract"
            )
        if verdict == "suspended":
            raise Inconclusive(
                "the covering adoption resolved with the suspended "
                "admission still `accepted` and unadjudicated — the "
                "suspended-entry duty the contract enumerates never "
                "ran; the pinned release predates the "
                "suspended-alias-audit contract"
            )
        if tamper == "vanished-admission":
            # The doctored expectation — the leg asserts the
            # suspended admission may vanish unaudited, the silent
            # aliased carry the finding drove. The honest adjudicated
            # record must fail it, naming the identity the admission
            # kept.
            failures.append(
                "the doctored expectation wanted the suspended "
                "admission vanished unaudited — "
                f"{ACTOR_SUSPENDED} kept its audit identity "
                f"({verdict}: {settles[duty_decl['name']]})"
            )
            raise Abort
        if verdict != "adjudicated":
            failures.append(
                f"the suspended admission {ACTOR_SUSPENDED} journaled "
                f"{settles[duty_decl['name']]} on its holder — one "
                "admission owes exactly one named superseded "
                "rejection, the adopted-window collision's only "
                "adjudication"
            )
        stray = settles[standby_decl["name"]]
        if stray:
            failures.append(
                "the promoted peer journaled "
                f"{stray} for the suspended admission it never "
                "held — the alias leaked across the peers"
            )
        for name, entries in identical_settles.items():
            if len(entries) != 1 or entries[0][2] != "applied":
                failures.append(
                    f"{name} journaled {entries} for the identical "
                    "admission — the covering path owes exactly one "
                    "applied settle per peer"
                )
        indexed = index_receipt(
            standby_url, alias_index, "GET /checkpoint", failures
        )
        evidence["identical_indexed"] = indexed
        if not admission_hit(indexed, command, ACTOR_IDENTICAL) or (
            simulate.receipt_outcome(indexed) != "applied"
        ):
            failures.append(
                f"the promoted peer's receipt at the collision index "
                f"{alias_index} reads {indexed} — the identical "
                "admission did not settle applied on its own index"
            )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "audit",
                "suspended": verdict,
                "settles": settles,
                "identical_settles": identical_settles,
            }
        )

        # Phase 9 — the durable half: each peer's declared journal
        # file carries the same one settle per admission the serving
        # monitor did — the adjudication reaching the durable record,
        # never a served-only boundary.
        durable = {}
        for name, files in (
            (duty_decl["name"], rig.duty_files),
            (standby_decl["name"], rig.standby_files),
        ):
            entries = journal_entries(files["journal_file"])
            durable[name] = {
                "suspended": admission_settles(
                    entries, command, ACTOR_SUSPENDED
                ),
                "identical": admission_settles(
                    entries, command, ACTOR_IDENTICAL
                ),
            }
        for name, record in durable.items():
            if record["suspended"] != settles[name]:
                failures.append(
                    f"{name}'s durable journal carries "
                    f"{record['suspended']} for the suspended "
                    "admission where its served journal carries "
                    f"{settles[name]} — the durable audit is not "
                    "the served audit"
                )
            if record["identical"] != identical_settles[name]:
                failures.append(
                    f"{name}'s durable journal carries "
                    f"{record['identical']} for the identical "
                    "admission where its served journal carries "
                    f"{identical_settles[name]} — the durable audit "
                    "is not the served audit"
                )
        if failures:
            raise Abort
        digest_entries.append({"phase": "durable", "durable": durable})

        # Phase 10 — the launch roles restored: demote the promoted
        # peer, promote the rejoined holder, and drive the pair back
        # to the manifest's declared arrangement — the duty
        # controller `active`, its standby `tracking` again.
        demote = rig.demote(
            standby_url, failures, "the promoted field owner"
        )
        restored = None
        for _ in range(WATCH_SCANS):
            pair.scan(standby_url, failures)
            report = pair.get(
                f"{standby_url}/role", "GET /role", failures
            )
            if report.get("role") == "standby":
                restored = report
                break
        if restored is None:
            failures.append(
                "the promoted peer never demoted for the restore — "
                f"GET /role answers {report}"
            )
            raise Abort
        promote = rig.promote(
            duty_url, failures, "the rejoined holder"
        )
        restore_ticks = []
        for _ in range(RESTORE_TICKS):
            _tracked, owner = rig.tick(
                standby_url,
                duty_url,
                failures,
                diverged="the restored pair's images diverged at "
                "tick {tick} — the launch-role restore was not "
                "bumpless",
            )
            restore_ticks.append(owner["tick"])
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        standby_role = pair.get(
            f"{standby_url}/role", "GET /role", failures
        )
        if duty_role.get("role") != "active":
            failures.append(
                f"the restored field owner reports "
                f"{duty_role.get('role')!r}, expected active — the "
                "pair was not left in its launch roles"
            )
        if not tracking(standby_role):
            failures.append(
                "the restored standby never reconverged tracking — "
                f"GET /role answers {standby_role}"
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
    except Inconclusive:
        raise
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
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
        choices=["vanished-admission"],
        help="doctor the leg's expectation to the defect shape — the "
        "pass must fail naming the identity the admission kept",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = suspended_alias_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "suspended-alias-audit: the doctored expectation "
                "wanted the suspended admission vanished — an "
                "inconclusive run offers the doctored case no "
                "evidence"
            )
            return 1
        eprint(f"suspended-alias-audit: inconclusive — {inconclusive}")
        print(
            f"suspended-alias-audit-digest inconclusive — "
            f"{inconclusive}"
        )
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"suspended-alias-audit: {line}")
        return 1
    for failure in failures:
        eprint(f"suspended-alias-audit: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"suspended-alias-audit: the {args.tamper} case "
                "passed silently — the leg never noticed the "
                "doctored expectation"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"suspended-alias-audit-digest {digest} — tracking by tick "
        f"{evidence['converged']}, the suspended admission "
        f"{evidence['verdict']} at index {evidence['indices']['duty']} "
        "against the identical adopted-window command — one named "
        "superseded settle on its holder, one applied settle per "
        "peer on the identical admission's own index, both durable "
        "journals carrying the same record, launch roles restored "
        f"at tick {evidence['restored_at']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
