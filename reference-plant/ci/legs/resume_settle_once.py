#!/usr/bin/env python3
"""The resume-settle-once leg for the reference plant — the
consumer-side proof that a receipted command suspended at the
holder's fenced demote and frozen into the quiesced peer's declared
`--state-file` never re-applies when that peer restarts as the
field-owning active: the successor's carried settle is the
admission's one terminal settlement, the point keeps the newer
command's value, and the rejoining incumbent's served verdict stays
the settled truth (WW-ENG-003, WW-LCM-001 — the #1056
settle-once-across-resume contract, mirrored at the customer
boundary from the qa rig's `resume-settle-once` scenario).

The demote-pending leg (`ci/legs/demote_pending.py`) proves a
pending command crossed by a documented switch settles exactly
once; the standby-restart leg (`ci/legs/standby_restart.py`) proves
a tracking peer's declared-files resume; the suspended-alias-audit
leg proves the adopted-window collision's adjudication. None of
them restarts a quiesced peer into a run whose suspended copy a
successor already applied — the finding's reproduction: a receipted
command on the field owner rolls back to `Accepted` at a rogue
claim's fenced demote, the suspended shape the demoted run's
cycle-end checkpoint persists stamped `source_owns_field: false`;
the promoted peer carries and applies it plus a newer command on
the same point; restarting the quiesced peer as the field-owning
active must not re-queue the restored receipt — on the defective
build it re-applied the stale command, stomping the newer settle's
value and minting a second terminal `command_settled` for one
admission, while the rejoining peer's adopted document regressed
its served verdict.

The driven pair makes the episode deterministic: the peers' pulls
live only inside their own `POST /scan`, so the leg stages the
suspension, the carry, and the freezes by which peers it scans and
which it stops. The run:

- converges the declared pair to `tracking` through the pair leg's
  driven-tick loop and gates the contract surface — each peer's
  served checkpoint carrying the receipt window, the admission
  counters, and the `source_owns_field` stamp, plus the manifest's
  declared persistence on both peers;
- submits a receipted `write_value` on the declared writable
  `p101-hand` point — a value flip the newer command's write-back
  separates — on the field owner, then preempts the field's writer
  claim with a rogue attachment's unconditional `claim_writer` and
  drives the superseded owner's detection scans until it settles
  `standby`: the frozen `--state-file` must hold the non-owning
  stamp beside the admission's still-`Accepted` receipt — the
  suspended document the restart resumes;
- drives the orphaned sibling's pulls until its served window
  carries the suspended admission still `Accepted` in a promotable
  posture, then stops the holder so the promote boundary's
  final-sync fetch has nothing live to pull;
- promotes the sibling — its claim preempting the dead-tool rogue —
  drives its first field-owning boundary applying the carried
  admission `applied`, and submits the newer same-point command
  settling `applied`: the line's one settlement per admission;
- stops the incumbent so its claim stands with no live holder,
  then restarts the quiesced holder on its declared files with no
  `--standby` wiring — the conditional startup grant preempting the
  dead incumbent's claim, resume-as-active — and drives its scans:
  the restored `Accepted` must park, never re-queue;
- rejoins the incumbent on its declared listen address wired at
  the resumed peer, driving tracking-first ticks until it
  reconverges — the adopted still-`Accepted` view of the admission
  must not regress its own settled verdict;
- audits both serving monitors and both declared durable journals:
  the point serves the newer command's value on both peers, each
  admission carries exactly one `command_settled` — `applied`,
  journaled on the successor alone, the resumed peer owing none —
  and the rejoined peer's served verdicts equal the pre-restart
  records. The launch roles — the manifest-declared duty controller
  `active`, its standby `tracking` — are the run's own end state.

The contract postdates the pinned v0.3.0 release: where the
launched tooling predates it the run's own evidence is the
pre-contract shape — a checkpoint carrying no receipt window,
admission counters, or ownership stamp, a receipt dropping its
declared `actor`/`reason`, a refused preemption, a demote settling
rather than suspending the admission, a frozen document carrying
no non-owning stamp, a carry that never forms, or a conditional
startup grant that never takes the dead incumbent's field — and
the leg reports `resume-settle-once-digest inconclusive` rather
than asserting until the manifest repins a release carrying the
contract.

Usage:

    resume_settle_once.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `resume-settle-once-digest <sha256>` line prints —
the check runs two passes and compares them
(`resume-settle-once-nondeterministic`). A contract violation
reports `resume-settle-once: …` lines on stderr and exits 1 — the
check's `resume-settle-once-failed`. `--tamper expect-reapply`
doctors the leg's own expectation to the defect shape — asserting
the resumed peer re-applies the suspended admission — so the leg
proves its parked-receipt assertion fires on the honest run rather
than passing an unexercised contract.
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
# The doctored case: a leg asserting the resumed peer may re-apply
# the suspended admission — the stale re-apply the contract closed
# — must surface the named diagnostic on the honest parked record
# rather than passing an unexercised contract.
LEG = {
    "order": 430,
    "title": "the resume-settle-once leg",
    "passes": "resume-settle-once",
    "tampers": [
        {
            "name": "expect-reapply",
            "passed": "an expect-reapply case passed the resume-settle-once leg",
            "missed": "the expect-reapply case did not report its named diagnostic",
            "evidence": ["the doctored expectation wanted the resumed peer re-applying"],
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
# token, distinct from the tokens the other legs stage.
CLAIM_ROGUE = 0xF05E

# The driven-scan bounds each transition gets: the fenced demote's
# detection scans, the carry's adoption, the promoted peer's
# convergence and settles, the resumed run's active report and the
# defect window's boundaries, and the rejoin's reconvergence.
WATCH_SCANS = 6
CARRY_SCANS = 6
RESUME_SCANS = 4
REJOIN_SCANS = 12

# The admissions' declared identities — unique in both peers' receipt
# logs, the (command, actor) pair the settle audit correlates by.
ACTOR_SUSPENDED = "ci-resume-susp"
ACTOR_NEWER = "ci-resume-newer"
REASON = "resume-settle-once"

# The promotable sync vocabulary a standby's RoleReport carries —
# tracking, orphaned, or reinitialized, the converged shapes
# `POST /promote` accepts; the orphaned shape is this leg's carrier.
PROMOTABLE = ("tracking", "orphaned", "reinitialized")


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


def admission_receipts(url, command, actor, what, failures):
    """The admission's entries in the peer's served `GET /receipts`
    log — the serving half of the audit."""
    receipts = pair.get(f"{url}/receipts", what, failures)
    return [
        entry for entry in receipts if admission_hit(entry, command, actor)
    ]


def checkpoint_surface(url, what, failures):
    """The contract surface the leg's correlation and the resume
    gate read: the served checkpoint's receipt window, admission
    counters, and `source_owns_field` stamp. Any absence is the
    release predating the contract, never a violation of it."""
    checkpoint = pair.get(f"{url}/checkpoint", what, failures)
    if (
        not isinstance(checkpoint.get("receipts"), list)
        or not isinstance(
            (checkpoint.get("command_admission") or {}).get("attempts"),
            int,
        )
        or "source_owns_field" not in checkpoint
    ):
        raise Inconclusive(
            "the served checkpoint carries no receipt window, "
            "admission counters, or ownership stamp — the pinned "
            "release predates the resume-settle contract: "
            f"{sorted(checkpoint)}"
        )
    return checkpoint


def state_checkpoint(path):
    """The controller's persisted `--state-file` checkpoint as a
    dict, or None while the file is absent or mid-rewrite."""
    try:
        with open(path) as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None


def tracking(report):
    """Whether a served RoleReport carries `standby` under the
    `tracking` sync state."""
    sync = report.get("sync") if isinstance(report, dict) else None
    return report.get("role") == "standby" and (
        isinstance(sync, dict) and "tracking" in sync
    )


def promotable(report):
    """Whether a served RoleReport holds a promotable standby
    posture — tracking, orphaned, or reinitialized. The orphaned
    shape is this leg's carrier: a standby whose tracked source
    demoted in place reports orphaned and keeps reporting it."""
    sync = report.get("sync") if isinstance(report, dict) else None
    return report.get("role") == "standby" and (
        isinstance(sync, dict)
        and bool(set(sync) & set(PROMOTABLE))
    )


def resume_settle_pass(args, tamper):
    """The resume-settle run: converge, admit, preempt, suspend,
    carry, promote and settle, restart the quiesced holder as the
    field-owning active, rejoin the incumbent, audit both monitors
    and both durable journals, assert the launch roles. Returns
    `(digest_entries, evidence, failures)`; raises `Inconclusive`
    where the pinned release predates the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "resume-settle-once leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    with open(args.model) as handle:
        model = json.load(handle)
    point = force_carryover.force_target(model)
    if point is None:
        raise Abort(
            f"the emitted model declares no writable In point at "
            f"{force_carryover.FORCE_SIGNAL} — the resume-settle "
            "leg has nothing to suspend"
        )

    digest_entries, evidence, failures = [], {}, []
    rig = None
    probe_io = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        duty_listen = duty_url.removeprefix("http://")
        standby_listen = standby_url.removeprefix("http://")

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
        # the receipt window, admission counters, and the
        # `source_owns_field` stamp — the holder's persisted document
        # the resume gate reads — and the pair's declared
        # persistence: the holder's state file the suspended receipt
        # freezes into, both journal files the durable half of the
        # settle audit. A surface absent is the release predating
        # the contract, never a violation of it.
        for name, url in (
            (duty_decl["name"], duty_url),
            (standby_decl["name"], standby_url),
        ):
            checkpoint_surface(url, f"GET /checkpoint on {name}", failures)
        if (
            rig.duty_files.get("state_file") is None
            or rig.duty_files.get("journal_file") is None
            or rig.standby_files.get("journal_file") is None
        ):
            raise Inconclusive(
                "the manifest's pair declares no "
                "state_file/journal_file persistence — the durable "
                "half of the audit and the holder's suspended "
                "capture are absent"
            )
        baseline = simulate.snapshot_point(owner, point)
        if baseline is None or "bool" not in baseline:
            raise Abort(
                f"the suspension target point {point} serves "
                f"{baseline} — a bool baseline the leg can flip is "
                "required"
            )
        # The two writes the stomp test separates: the carried
        # command's value differs from the newer command's — the
        # resumed peer re-applying the stale one is the defective
        # image's signature — and the newer write lands the baseline
        # back so the rolled-back demotion image already equals the
        # surviving verdict.
        first = takeover.write_value(point, not baseline["bool"])
        second = takeover.write_value(point, baseline["bool"])
        second_value = {"bool": baseline["bool"]}
        digest_entries.append(
            {
                "phase": "gate",
                "claim": "fenced",
                "surface": "present",
                "persistence": "declared",
            }
        )

        # Phase 2 — the suspended admission: the receipted write on
        # the still-active field owner inside the window the rogue
        # claim opens — submitted first, never scanned, so it stands
        # pending when the preemption lands.
        status, suspended = pair.request(
            f"{duty_url}/command",
            {
                "command": first,
                "actor": ACTOR_SUSPENDED,
                "reason": REASON,
            },
        )
        evidence["suspended_submission"] = {
            "status": status,
            "receipt": suspended,
        }
        if status != 200 or simulate.receipt_outcome(suspended) != "accepted":
            failures.append(
                f"the suspended admission answered {status} "
                f"{suspended}, expected an accepted receipt"
            )
            raise Abort
        if (
            suspended.get("actor") != ACTOR_SUSPENDED
            or suspended.get("reason") != REASON
        ):
            raise Inconclusive(
                "the served receipt drops the declared actor/reason "
                "— the submission-record identity the settle audit "
                "correlates by; the pinned release predates the "
                "resume-settle contract"
            )
        digest_entries.append(
            {
                "phase": "suspend",
                "point": point,
                "command": first,
                "receipt": suspended,
            }
        )

        # Phase 3 — the preemption the fenced demote hangs on: a
        # rogue attachment takes the field claim under the standing
        # owner, so the detection scan demotes the holder in place —
        # and while the monitor-less rogue stands the demoted peer
        # can prove no tracking source, so no covering adoption
        # reaches the suspended file. `controller: false` marks the
        # hold a tool's, never a peer's: the orphaned sibling's
        # promote and the restarted run's startup grant are both
        # conditional takeovers — the field's own arbitration of a
        # live incumbent against a dead or rogue hold — which a live
        # controller-marked claim would refuse but a tool's never
        # does. The rig's own client stays read-only, so the claim
        # belongs to this one connection.
        probe_io = simulate.PlantClient(rig.plant_addr)
        preempt = probe_io.request(
            {
                "op": "claim_writer",
                "owner": CLAIM_ROGUE,
                "controller": False,
            }
        )
        evidence["preempt"] = preempt
        if preempt.get("result") != "done":
            raise Inconclusive(
                "the rogue claim answered "
                f"{preempt.get('result')} — the consumer harness "
                "admits no unconditional preemption lever, or the "
                "pinned plant predates it"
            )

        # Phase 4 — the demote-in-place watch: the superseded owner's
        # detection scans walk demoting to standby, the fenced
        # boundary's suspend replaying the admission back to
        # `Accepted` — the frozen `--state-file` then holding the
        # non-owning stamp beside the still-suspended receipt. A
        # demote that settles the admission instead of suspending
        # it is the pre-contract shape.
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
        document = state_checkpoint(rig.duty_files["state_file"])
        evidence["frozen_document"] = {
            "owns_field": (document or {}).get("source_owns_field"),
        }
        if document is None or "source_owns_field" not in document:
            raise Inconclusive(
                "the demoted peer's persisted document carries no "
                "ownership stamp — the resume gate the contract "
                "reads; the pinned release predates the "
                "suspended-resume contract"
            )
        if document.get("source_owns_field") is not False:
            raise Inconclusive(
                "the fenced demote's non-owning checkpoint never "
                "reached the state file — the pinned release "
                "predates the suspended-resume contract"
            )
        frozen = next(
            (
                entry
                for entry in document.get("receipts") or []
                if admission_hit(entry, first, ACTOR_SUSPENDED)
            ),
            None,
        )
        evidence["frozen_receipt"] = frozen
        if not admission_hit(frozen, first, ACTOR_SUSPENDED):
            raise Inconclusive(
                "the demoted peer's frozen document lost the "
                "suspended admission outright — the pinned release "
                "predates the suspended-entry contract"
            )
        if simulate.receipt_outcome(frozen) != "accepted":
            raise Inconclusive(
                "the demote recorded the admission "
                f"{simulate.receipt_outcome(frozen)} in the frozen "
                "document instead of suspending it — the pinned "
                "release predates the suspended-entry contract"
            )
        digest_entries.append(
            {
                "phase": "demote",
                "watch": roles,
                "frozen": "accepted",
            }
        )

        # Phase 5 — the suspension's carry half: the orphaned
        # sibling's driven pulls keep adopting the demoted
        # checkpoint — the suspended receipt lands in its window
        # still `Accepted`, its posture staying promotable through
        # the stop that follows.
        carried = None
        postures = []
        for _ in range(CARRY_SCANS):
            pair.scan(standby_url, failures)
            found = admission_receipts(
                standby_url, first, ACTOR_SUSPENDED,
                "GET /receipts", failures,
            )
            report = pair.get(
                f"{standby_url}/role", "GET /role", failures
            )
            sync = report.get("sync")
            postures.append(
                next(iter(sync), "unsynchronized")
                if isinstance(sync, dict)
                else sync
            )
            if found and promotable(report):
                carried = {"receipt": found[0], "report": report}
                break
        evidence["carry"] = {
            "postures": postures,
            "receipt": (carried or {}).get("receipt"),
        }
        if carried is None:
            raise Inconclusive(
                "the sibling never adopted the suspended admission "
                "into a promotable posture — the carry the resume "
                "leaves to the successor never formed; the pinned "
                "release predates the suspended-carry contract"
            )
        if simulate.receipt_outcome(carried["receipt"]) != "accepted":
            raise Inconclusive(
                "the sibling's quiesced scans settled the carried "
                "admission "
                f"{simulate.receipt_outcome(carried['receipt'])} — "
                "the pinned release predates the suspended-carry "
                "contract"
            )
        digest_entries.append(
            {
                "phase": "carry",
                "postures": postures,
                "carried": "accepted",
            }
        )

        # Phase 6 — the freeze and the successor's promote: the
        # holder stops so the promote boundary's final-sync fetch
        # has nothing live to pull — the settled line is the
        # tracking pull's own carry — and the orphaned peer's
        # promote preempts the rogue, claiming the field.
        pair.stop(rig.duty)
        status, promote = pair.request(f"{standby_url}/promote", {})
        evidence["promote"] = {"status": status, "body": promote}
        if status != 200:
            failures.append(
                f"POST /promote on the orphaned sibling answered "
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
                "the promoted sibling never settled into the active "
                f"role — GET /role answers {report}"
            )
            raise Abort

        # The carried admission's one settlement: the promoted
        # peer's first field-owning boundary applies what the carry
        # held — the suspended line owed exactly one `applied`.
        outcome = "accepted"
        for _ in range(WATCH_SCANS):
            found = admission_receipts(
                standby_url, first, ACTOR_SUSPENDED,
                "GET /receipts", failures,
            )
            if found:
                outcome = simulate.receipt_outcome(found[0])
                if outcome != "accepted":
                    break
            pair.scan(standby_url, failures)
        carried_settles = admission_settles(
            pair.get(
                f"{standby_url}/journal", "GET /journal", failures
            ),
            first,
            ACTOR_SUSPENDED,
        )
        evidence["carried_settle"] = {
            "outcome": outcome,
            "settles": carried_settles,
        }
        if outcome != "applied":
            failures.append(
                "the carried admission settled "
                f"{outcome} on the successor — the suspended line "
                "owed exactly one applied"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "promote",
                "promote": promote,
                "carried_settle": "applied",
            }
        )

        # Phase 7 — the newer command on the same point: the verdict
        # the stale re-apply would stomp.
        status, newer = pair.request(
            f"{standby_url}/command",
            {
                "command": second,
                "actor": ACTOR_NEWER,
                "reason": REASON,
            },
        )
        evidence["newer_submission"] = {
            "status": status,
            "receipt": newer,
        }
        if status != 200 or simulate.receipt_outcome(newer) != "accepted":
            failures.append(
                f"the newer submission on the successor answered "
                f"{status} {newer}"
            )
            raise Abort
        outcome = "accepted"
        for _ in range(WATCH_SCANS):
            pair.scan(standby_url, failures)
            found = admission_receipts(
                standby_url, second, ACTOR_NEWER,
                "GET /receipts", failures,
            )
            if found:
                outcome = simulate.receipt_outcome(found[0])
                if outcome != "accepted":
                    break
        evidence["newer_settle"] = outcome
        if outcome != "applied":
            failures.append(
                "the newer command settled "
                f"{outcome} on the successor — its apply boundary "
                "owed exactly one applied"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "newer",
                "receipt": newer,
                "settle": "applied",
            }
        )

        # The pre-resume truth: the surviving line's served verdicts
        # for both admissions, captured before the restart so the
        # rejoin's stability check compares the same records.
        before = {}
        for key, command, actor in (
            ("suspended", first, ACTOR_SUSPENDED),
            ("newer", second, ACTOR_NEWER),
        ):
            found = admission_receipts(
                standby_url, command, actor,
                "GET /receipts", failures,
            )
            before[key] = found[0] if found else None
        evidence["before_restart"] = before

        # Phase 8 — the incumbent freezes: the successor's stop
        # leaves its claim standing with no live holder — the
        # dead-owner shape the conditional startup grant preempts —
        # and its own `--state-file` carries the settled verdicts
        # the rejoin resumes.
        pair.stop(rig.standby)

        # Phase 9 — the rig's restart: the demoted run's suspended
        # checkpoint resumes restart-as-active — its restored
        # `Accepted` must park for the line's adjudication rather
        # than re-queue.
        rig.duty, url, preamble = pair.spawn_peer(
            args.controller,
            args.model,
            args.dt,
            rig.plant_addr,
            None,
            rig.duty_files,
            listen=duty_listen,
            pair_token=pair.PAIR_TOKEN,
        )
        rig.duty_url = url
        evidence["resume_preamble"] = preamble
        if url is None:
            raise Inconclusive(
                "the restarted peer exited at startup: "
                f"{'; '.join(preamble[-2:]) or 'no diagnostic'} — "
                "the conditional startup grant never took the dead "
                "incumbent's field"
            )
        duty_url = rig.duty_url
        resumed = None
        for _ in range(WATCH_SCANS):
            report = pair.get(f"{duty_url}/role", "GET /role", failures)
            if report.get("role") == "active":
                resumed = report
                break
            pair.scan(duty_url, failures)
        evidence["resumed"] = resumed
        if resumed is None:
            raise Inconclusive(
                "the restarted peer never reported active — the "
                "conditional startup grant never took the dead "
                "incumbent's field; the pinned release predates "
                "the resume contract"
            )

        # The defect window: give the resumed run a few boundaries —
        # long enough for a re-queued stale command to re-apply and
        # journal — then read what it did with the restored receipt.
        for _ in range(RESUME_SCANS):
            pair.scan(duty_url, failures)
        suspended_after = admission_receipts(
            duty_url, first, ACTOR_SUSPENDED,
            "GET /receipts", failures,
        )
        suspended_after = suspended_after[0] if suspended_after else None
        evidence["resumed_receipt"] = suspended_after
        digest_entries.append(
            {
                "phase": "resume",
                "role": resumed.get("role"),
                "tick": resumed.get("tick"),
            }
        )

        # Phase 10 — the incumbent rejoins: its own persisted run
        # resumes — applied verdicts and image included — and
        # tracks the restarted peer's line. The adopted
        # still-`Accepted` view of the admission must not regress
        # the run's own settled verdict.
        rig.standby, url, preamble = pair.spawn_peer(
            args.controller,
            args.model,
            args.dt,
            rig.plant_addr,
            duty_url.removeprefix("http://"),
            rig.standby_files,
            listen=standby_listen,
            pair_token=pair.PAIR_TOKEN,
        )
        rig.standby_url = url
        if url is None:
            raise Inconclusive(
                "the incumbent's restart exited at startup: "
                f"{'; '.join(preamble[-2:]) or 'no diagnostic'}"
            )
        standby_url = rig.standby_url
        rejoined = None
        for _ in range(REJOIN_SCANS):
            pair.scan(standby_url, failures)
            pair.scan(duty_url, failures)
            peer_report = pair.get(
                f"{standby_url}/role", "GET /role", failures
            )
            owner_report = pair.get(
                f"{duty_url}/role", "GET /role", failures
            )
            if tracking(peer_report) and (
                owner_report.get("role") == "active"
            ):
                rejoined = (peer_report, owner_report)
                break
        evidence["rejoined"] = rejoined
        if rejoined is None:
            failures.append(
                "the restarted incumbent never reconverged tracking "
                "behind the resumed peer"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "rejoin",
                "peer_sync": "tracking",
                "owner_role": "active",
            }
        )

        # Phase 11 — the audit: every clause reads the pair's
        # served surfaces or the durable journal files — the
        # externally visible shape of the contract, never the
        # private mark the runtime parks the receipt under.
        digest = {}
        after = {}
        for key, command, actor in (
            ("suspended", first, ACTOR_SUSPENDED),
            ("newer", second, ACTOR_NEWER),
        ):
            found = admission_receipts(
                standby_url, command, actor,
                "GET /receipts", failures,
            )
            after[key] = found[0] if found else None
        evidence["after_rejoin"] = after
        images = {
            name: simulate.snapshot_point(
                pair.get(f"{url}/snapshot", "GET /snapshot", failures),
                point,
            )
            for name, url in (
                (duty_decl["name"], duty_url),
                (standby_decl["name"], standby_url),
            )
        }
        evidence["images"] = images
        admissions = (
            ("suspended", first, ACTOR_SUSPENDED),
            ("newer", second, ACTOR_NEWER),
        )
        served_journals = {
            name: pair.get(f"{url}/journal", "GET /journal", failures)
            for name, url in (
                (duty_decl["name"], duty_url),
                (standby_decl["name"], standby_url),
            )
        }
        journaled = {
            name: {
                key: admission_settles(journal, command, actor)
                for key, command, actor in admissions
            }
            for name, journal in served_journals.items()
        }
        durable = {
            name: {
                key: admission_settles(
                    journal_entries(path), command, actor
                )
                for key, command, actor in admissions
            }
            for name, path in (
                (duty_decl["name"], rig.duty_files["journal_file"]),
                (standby_decl["name"], rig.standby_files["journal_file"]),
            )
        }
        evidence["journaled"] = journaled
        evidence["durable"] = durable

        # The point keeps the newer command's value on both monitors
        # — the stale re-apply's stomp is the defective image's only
        # signature.
        stomped = [
            name for name, value in images.items() if value != second_value
        ]
        if stomped:
            digest["point"] = "stomped"
            failures.append(
                f"the point serves {images} — not the newer "
                f"command's value {second_value}; the restarted "
                "run's stale apply shows on "
                f"{stomped}"
            )
        else:
            digest["point"] = "newer"

        # The restored receipt stays honestly `Accepted` on the
        # resumed peer — parked, never re-queued.
        if suspended_after is None:
            digest["resumed"] = "vanished"
            failures.append(
                "the restarted peer's log lost the suspended "
                "admission outright"
            )
        elif tamper == "expect-reapply":
            # The doctored expectation — the leg asserts the
            # resumed peer re-applies the suspended admission, the
            # stale re-apply the contract closed. The honest parked
            # record must fail it.
            failures.append(
                "the doctored expectation wanted the resumed peer "
                "re-applying the suspended admission — the honest "
                "run left it "
                f"{simulate.receipt_outcome(suspended_after)}"
            )
        elif simulate.receipt_outcome(suspended_after) == "accepted":
            digest["resumed"] = "parked"
        else:
            digest["resumed"] = "reapplied"
            failures.append(
                "the restarted peer's receipt settled "
                f"{simulate.receipt_outcome(suspended_after)} — "
                "the restored admission re-queued instead of "
                "parking"
            )

        # The rejoining peer's served verdicts are the pre-restart
        # records — never regressed to the adopted `Accepted`,
        # never rewritten to a restarted run's settle.
        for key in ("suspended", "newer"):
            pre, post = before[key], after[key]
            if pre is None or post is None:
                digest[key + "_verdict"] = "lost"
                failures.append(
                    "the rejoined peer's log lost the "
                    f"{key} admission's receipt"
                )
            elif post.get("outcome") != pre.get("outcome"):
                digest[key + "_verdict"] = "mutated"
                failures.append(
                    f"the {key} admission's served verdict mutated "
                    "across the rejoin: "
                    f"{json.dumps(pre.get('outcome'))} -> "
                    f"{json.dumps(post.get('outcome'))}"
                )
            else:
                digest[key + "_verdict"] = "stable"

        # One admission, one terminal settlement across the pair's
        # journals — served and durable alike: the suspended
        # admission's single `applied` lives on the successor's
        # record alone; the resumed peer owes it none.
        counts = {}
        journal_failures = []
        for name in (duty_decl["name"], standby_decl["name"]):
            counts[name] = {}
            expected = 1 if name == standby_decl["name"] else 0
            for key in ("suspended", "newer"):
                served = journaled[name][key]
                filed = durable[name][key]
                counts[name][key] = {
                    "served": len(served),
                    "durable": len(filed),
                }
                if len(served) != expected or len(filed) != expected:
                    journal_failures.append(
                        f"{name} journaled {len(served)} served / "
                        f"{len(filed)} durable command_settled "
                        f"records for the {key} admission — the "
                        f"contract owes exactly {expected} there"
                    )
                elif expected == 1 and (
                    served[0][2] != "applied" or filed[0][2] != "applied"
                ):
                    journal_failures.append(
                        f"{name}'s {key} admission settled "
                        f"{served[0][2]} served / {filed[0][2]} "
                        "durable — the line's one settlement is "
                        "applied"
                    )
        evidence["counts"] = counts
        digest["journals"] = "split" if journal_failures else "once-each"
        failures.extend(journal_failures)
        if failures:
            raise Abort

        # The launch roles: the resumed owner holds the field, the
        # rejoined incumbent tracks it — the layout the legs behind
        # this one enter on.
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        standby_role = pair.get(
            f"{standby_url}/role", "GET /role", failures
        )
        restored = duty_role.get("role") == "active" and tracking(
            standby_role
        )
        if not restored:
            digest["roles"] = "unrestored"
            failures.append(
                "the pair did not land back on the launch roles — "
                f"owner {duty_role.get('role')!r}, standby "
                f"{standby_role.get('sync')}"
            )
            raise Abort
        digest["roles"] = "restored"
        evidence["restored_at"] = duty_role.get("tick")
        digest_entries.append({"phase": "audit", **digest})
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Inconclusive:
        raise
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        # Detach the claim attachment cleanly: release whatever hold
        # it still carries — a hold left standing keeps the field
        # claimed for a dead token — then close.
        if probe_io is not None:
            try:
                probe_io.request({"op": "release_writer"})
            except Exception:
                pass
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
        choices=["expect-reapply"],
        help="doctor the leg's expectation to the defect shape — the "
        "pass must fail naming the parked record the honest run "
        "left",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = resume_settle_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "resume-settle-once: the doctored expectation wanted "
                "the resumed peer re-applying the suspended "
                "admission — an inconclusive run offers the doctored "
                "case no evidence"
            )
            return 1
        eprint(f"resume-settle-once: inconclusive — {inconclusive}")
        print(
            f"resume-settle-once-digest inconclusive — {inconclusive}"
        )
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"resume-settle-once: {line}")
        return 1
    for failure in failures:
        eprint(f"resume-settle-once: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"resume-settle-once: the {args.tamper} case passed "
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
        f"resume-settle-once-digest {digest} — tracking by tick "
        f"{evidence['converged']}, the suspended admission parked "
        "across the holder's restart-as-active, one command_settled "
        "per admission across both peers' served and durable "
        "journals, the rejoined incumbent's verdicts stable, launch "
        f"roles restored at tick {evidence['restored_at']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
