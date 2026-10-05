#!/usr/bin/env python3
"""The standby-loss leg for the reference plant — the consumer-side
mirror of the rig's standby-loss scenario (`1700_standby_loss`,
scenario key `standby-loss`, requirements WW-ENG-003, WW-LCM-001 and
WW-OPS-003): the customer-owned pair's role-gated refusals and its
non-interference under a lost standby, proven on the deployment
`deploy/manifest.json` actually declares rather than only on the rig.

The pair leg (`ci/legs/pair.py`) proves the declared pair runs,
receipts a kind-declared command, and switches on the documented
demote/promote order; the refusal leg (`ci/legs/refusal.py`) proves
the standby-directed write is refused while the standby tracks; the
standby-restart leg (`ci/legs/standby_restart.py`) proves the
standby resumes onto its declared state file and reconverges. None
stages standby *loss*: no consumer leg stops the standby's process and
watches what the controller of record does about it. Peer loss is not
an event the controller of record reacts to — the field owner must
keep scanning, keep settling receipted commands, keep its role, and
journal nothing role-side across the whole down window. This leg runs
that episode on the manifest-declared pair, ordered by the driven
harness — a peer applies live only inside its own `POST /scan`, so
every leg below is the harness's to hold open:

- converges the declared pair to `tracking` and picks the writable
  boolean point the emitted model declares, then settles a kind-
  declared `invoke` receipted `applied` into both peers' adopted
  receipt log — the pre-loss run leaving journaled entries the
  non-interference audit must not disturb;
- (a) the role-gated refusal on the *tracking* standby: a receipted
  `write_value` submitted to the tracking standby's monitor must
  answer the named `not_active` rejection — the point unchanged in
  the field owner's served snapshot and in the simulated plant's own
  stored sample, the write absent from both peers' adopted receipt
  logs, and no `command_settled` record on the field owner at all
  while the standby's echoes the named rejection alone. A standby
  that settles a field mutation is exactly the pair-semantics
  dishonesty the publication boundary exists to prevent;
- (b) the down window: the tracking standby's process stops and the
  field owner is driven throughout — each scan's writes landing on
  the simulated field, a second receipted command settling `applied`
  on the owner, its served tick strictly advancing, its role staying
  `active`, and its served and durable journals gaining no
  `role_changed` and no `run_boundary` record across the window. Peer
  loss is not an event the controller of record reacts to, so an
  active disturbed by its peer's loss is the defect;
- (c) the return: the standby relaunches on the manifest's wiring and
  its declared persistence, rejoins `standby` unsynchronized, and
  reconverges to `tracking` inside the leg's declared window while
  the field owner keeps writing;
- (d) the premature promote: `POST /promote` fired at the returning
  monitor the moment it first answers — before its first transfer has
  completed. A monitor still serving the unsynchronized verdict has
  nothing for the convergence gate to hold, so the request must
  answer the named `not_converged` refusal; a monitor that already
  answers `tracking` — the shape a peer resuming its declared
  persistence carries — has earned the promotion it is asking for, and
  the leg records that disposition and hands the field back through the
  documented order rather than reading it as a defect. Either way the
  gate read is a convergence gate, not a dead monitor: both verdicts
  come off a served `/role`;
- restores the pair's launch roles through tracking-first ticks so the
  legs behind this one start where it found them.

The contract lands with the release line an artifact set carries; a
pinned release predating it — the launched active recording no
owner-token claim line, the declared pair carrying no `journal_file`
persistence, or the served role report carrying no sync vocabulary —
is the pre-contract shape, and the leg reports
`standby-loss-digest inconclusive` rather than asserting until the
manifest repins a release carrying the contract.

Usage:

    standby_loss.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `standby-loss-digest <sha256>` line prints — the check
runs two passes and compares them (`standby-loss-nondeterministic`). A
contract violation reports `standby-loss: …` lines on stderr and exits
1 — the check's `standby-loss-failed`. The `--tamper` cases doctor the
leg's own expectations, and each must fail carrying its named
evidence: `expect-applied` wants the standby-directed write settled
`applied`, `expect-peer-reaction` wants the active to leave `active`
across the down window, `expect-open-gate` wants the premature promote
admitted, `skip-restart` never relaunches the standby but keeps the
reconvergence assertions, and `skip-down-window` never stops the
standby but keeps the non-interference assertions.
"""

import argparse
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import pair
import refusal
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its two
# digest-identical passes, and exercise its doctored cases.
LEG = {
    # The next free slot in the pair stage's recorded order after the
    # legs origin/main added beside the release-line claim cluster
    # (responsiveness 860, cause-alarm-quality 870).
    "order": 880,
    "title": "the standby-loss leg",
    "passes": "standby-loss",
    "tampers": [
        {
            "name": "expect-applied",
            "passed": "a doctored write expectation passed the standby-loss leg",
            "missed": "the expect-applied case did not report its named diagnostic",
            "evidence": ["expected the standby-directed write to settle applied"],
        },
        {
            "name": "expect-peer-reaction",
            "passed": "a doctored peer-reaction expectation passed the standby-loss leg",
            "missed": "the expect-peer-reaction case did not report its named diagnostic",
            "evidence": ["expected the field owner to leave active"],
        },
        {
            "name": "expect-open-gate",
            "passed": "a doctored promote-gate expectation passed the standby-loss leg",
            "missed": "the expect-open-gate case did not report its named diagnostic",
            "evidence": ["expected the premature promote to be admitted"],
        },
        {
            "name": "skip-restart",
            "passed": "a skip-restart passed the standby-loss leg",
            "missed": "the skip-restart case did not report its named diagnostic",
            "evidence": ["never reconverged"],
        },
        {
            "name": "skip-down-window",
            "passed": "a skip-down-window passed the standby-loss leg",
            "missed": "the skip-down-window case did not report its named diagnostic",
            "evidence": ["the down window never stood"],
        },
    ],
}


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The pinned release predates the contract the leg exercises —
    the run classifies inconclusive, never a product failure. The first
    arg is the stable reason the `inconclusive` digest line prints;
    the optional second arg is the run's own evidence, reported on
    stderr only."""


# The driven-scan bounds each phase gets: the refused-write settling
# tick, the down window the field owner rides out, the returning
# peer's reconvergence, and the settle train that restores the launch
# roles.
SETTLE_SCANS = 4
DOWN_WINDOW_SCANS = 4
RECONVERGE_SCANS = 6
SETTLE_TICKS = 4
# The actor the leg's receipted submissions declare.
ACTOR = "ci-standby-loss"


def tracking(report):
    """Whether a served RoleReport reads `standby` under `tracking`
    sync."""
    sync = report.get("sync")
    return report.get("role") == "standby" and (
        isinstance(sync, dict) and "tracking" in sync
    )


def sync_kind(report):
    """The served StandbySync's variant name — `tracking`,
    `orphaned`, `degraded` — or None when the report carries none."""
    sync = report.get("sync")
    if isinstance(sync, str):
        return sync
    return next(iter(sync)) if isinstance(sync, dict) and sync else None


def role(url, failures):
    """The peer's served RoleReport."""
    return pair.get(f"{url}/role", "GET /role", failures)


def writable_point(model):
    """The writable boolean point the emitted model declares — the
    point the role-gated write legs target. None when the model
    declares no such point."""
    points = [
        point["id"]
        for point in sorted(model.get("io_points", []),
                            key=lambda entry: entry["id"])
        if point.get("writable") and point.get("value_type") == "bool"
    ]
    return points[0] if points else None


def declared_command(url, failures):
    """One `(component, spec, command)` the peer's served registry
    declares natively — the `invoke` the leg's settling submissions
    use."""
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


def settled(receipts, command):
    """The applied receipts one submitted command settled into a served
    receipt log."""
    return [
        entry
        for entry in receipts
        if entry.get("command") == command
        and simulate.receipt_outcome(entry) == "applied"
    ]


def command_receipts(entries, command):
    """The `command_settled` receipts a journal-entry list carries for
    one submitted command — the durable half of the refusal audit."""
    return refusal.command_receipts(entries, command)


def field_check(owner, plant_io, failures, when):
    """The field-mismatch audit one driven owner scan leaves — every
    simulated `out` point holding the owner's served image."""
    field = refusal.field_out_samples(plant_io)
    for mismatch in refusal.field_mismatches(owner, field):
        failures.append(f"{mismatch} {when} — the field owner's writes faltered")
    if failures:
        raise Abort
    return field


def settle_command(rig, duty_url, standby_url, failures):
    """Drive the settling ticks one receipted submission needs: the
    tracking peer first, so the pair's images reconverge identical and
    the adopted receipt logs read as one log. Returns the field
    owner's served snapshot."""
    for _ in range(SETTLE_SCANS):
        _tracked, owner = rig.tick(standby_url, duty_url, failures)
        if tracking(role(standby_url, failures)):
            break
    else:
        failures.append(
            "the tracking peer never reconverged after the command's "
            "settling ticks"
        )
        raise Abort
    return owner


def refusal_leg(duty_url, standby_url, point, plant_io, tamper, failures):
    """(a) The role-gated refusal on the tracking standby: a receipted
    `write_value` aimed at the standby answers the named `not_active`
    rejection with no field effect and no journaled command on the
    field owner. Returns the audit record."""
    before = simulate.snapshot_point(
        pair.get(f"{duty_url}/snapshot", "GET /snapshot", failures), point
    )
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
        if status != 200 or simulate.receipt_outcome(receipt) != "applied":
            failures.append(
                f"{TAMPER_APPLIED} — the standby-directed write answered "
                f"{status} {simulate.receipt_outcome(receipt)}"
            )
    elif status != 200 or "not_active" not in reason:
        failures.append(
            f"the tracking standby's role boundary answered {status} "
            f"{receipt}, expected a rejected not_active receipt — a "
            "standby settling a field mutation is the pair-semantics "
            "dishonesty the publication boundary exists to prevent"
        )
        raise Abort
    owner = pair.get(f"{duty_url}/snapshot", "GET /snapshot", failures)
    after = simulate.snapshot_point(owner, point)
    if after != before:
        failures.append(
            f"point {point} reads {after} in the field owner's served "
            f"snapshot after the standby-directed write, expected "
            f"{before} — the refused write reached the field owner"
        )
        raise Abort
    # The field-side half of the same audit: every simulated `out`
    # point still holds the field owner's served image. The written
    # point itself is a field *input* the plant's dynamics drive, so
    # its stored sample moves with the owner's own scans; the outputs
    # are the field image a second writer would show up in.
    field = refusal.field_out_samples(plant_io)
    for mismatch in refusal.field_mismatches(owner, field):
        failures.append(
            f"{mismatch} after the standby-directed write — the refusal "
            "had a field effect"
        )
        raise Abort
    audit = {}
    for name, url in (("owner", duty_url), ("peer", standby_url)):
        records = {
            "served": command_receipts(
                pair.get(f"{url}/journal", "GET /journal", failures),
                write,
            )
        }
        audit[name] = records
    if audit["owner"]["served"]:
        failures.append(
            f"the field owner's journal carries a command record for the "
            f"refused write: {audit['owner']['served']}"
        )
        raise Abort
    leaked = [
        entry
        for entry in audit["peer"]["served"]
        if simulate.receipt_outcome(entry) != "not_active"
    ]
    if leaked:
        failures.append(
            "the standby's journal records the refused write as "
            f"something but the named rejection: {leaked}"
        )
        raise Abort
    return {"write": write, "receipt": receipt, "point": point,
            "field": field, "audit": audit}


# The doctored cases' stable evidence prefixes — every failure the
# tampered pass records carries one, so the check's negative case finds
# it whether the honest run held or a predating release offered the
# doctored case no evidence at all.
TAMPER_APPLIED = (
    "expected the standby-directed write to settle applied"
)
TAMPER_PEER_REACTION = "expected the field owner to leave active"
TAMPER_OPEN_GATE = "expected the premature promote to be admitted"


def _role_entries(path):
    """The role-bearing entries and the run boundaries a declared
    `--journal-file` carries — the non-interference audit's own
    evidence, read once and diffed against its pre-loss floors."""
    role_entries, boundaries = [], []
    for kind, record in pair.journal_records(path):
        if kind == "boundary":
            boundaries.append(record)
        elif "role_changed" in record.get("event", {}) or (
            "run_boundary" in record.get("event", {})
        ):
            role_entries.append(record)
    return role_entries, boundaries


def standby_loss_pass(args, tamper):
    """The standby-loss run: converge and settle a command, the
    role-gated refusal on the tracking standby, the down window, the
    standby's relaunch and reconvergence, the premature promote's
    named refusal, and the restore of the pair's launch roles. Returns
    `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the standby-loss "
            "leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    with open(args.model) as handle:
        model = json.load(handle)
    point = writable_point(model)
    if point is None:
        raise Abort(
            "the emitted model declares no writable boolean point — "
            "the standby-loss leg has nothing to exercise"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = None
    duty_url = standby_url = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        duty_files, standby_files = rig.duty_files, rig.standby_files
        plant_io = rig.plant_io
        if duty_files.get("journal_file") is None or (
            standby_files.get("journal_file") is None
        ):
            raise Inconclusive(
                "the manifest's pair declares no journal files — the "
                "durable half of the non-interference audit is absent"
            )

        # Phase 1 — convergence and a settled command: the pre-loss run
        # leaving journaled entries the down-window audit must not
        # disturb.
        converged = rig.converge(failures)
        evidence["converged"] = converged["ticks"][-1]
        if not isinstance(converged["standby_role"].get("sync"), dict):
            raise Inconclusive(
                f"the declared standby's served report carries no sync "
                f"vocabulary this leg can read: "
                f"{json.dumps(converged['standby_role'])[:300]}"
            )
        component, spec, command = declared_command(duty_url, failures)
        submit(duty_url, command, failures)
        settle_command(rig, duty_url, standby_url, failures)
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
        if not settled(receipts_duty, command):
            failures.append(
                f"the declared command {spec['name']} on {component} "
                "never settled applied into the adopted receipt log"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"].get("role"),
                "standby_role": converged["standby_role"].get("role"),
                "command": spec["name"],
            }
        )

        # Phase 2 — the role-gated refusal on the tracking standby.
        audit = refusal_leg(
            duty_url, standby_url, point, plant_io, tamper, failures
        )
        evidence["refused_on"] = audit["point"]
        digest_entries.append({"phase": "refusal", **audit})

        # Phase 3 — the down window: the tracking standby's process
        # stops while the field owner is driven throughout. Peer loss is
        # not an event the controller of record reacts to.
        pre_role = role(duty_url, failures)
        pre_journal = pair.get(
            f"{duty_url}/journal", "GET /journal", failures
        )
        entry_floor, boundary_floor = _role_entries(
            duty_files["journal_file"]
        )
        entry_floor, boundary_floor = len(entry_floor), len(boundary_floor)
        snapshot = pair.get(
            f"{duty_url}/snapshot", "GET /snapshot", failures
        )
        tick0 = snapshot.get("tick")
        stopped = tamper != "skip-down-window"
        if stopped:
            pair.stop(rig.standby)
        # The down window must actually stand: the stopped peer's
        # monitor has to stop answering, or the non-interference audit
        # below proves nothing about peer loss.
        if _try_role(standby_url) is not None:
            failures.append(
                "the tracking standby's monitor kept answering after "
                "its process stopped — the down window never stood, so "
                "the non-interference audit read a running pair"
            )
            raise Abort
        _component, _spec, down_command = declared_command(duty_url,
                                                           failures)
        down_receipt = submit(duty_url, down_command, failures)
        scans = []
        window_settled = False
        previous = tick0
        for _ in range(DOWN_WINDOW_SCANS):
            owner = pair.scan(duty_url, failures)
            field = field_check(
                owner, plant_io, failures,
                "while the standby was down",
            )
            served_role = role(duty_url, failures)
            scans.append(
                {
                    "tick": owner["tick"],
                    "role": served_role.get("role"),
                    "field": field,
                }
            )
            if tamper == "expect-peer-reaction":
                # The doctored expectation: the active leaves `active`
                # across its peer's loss. The honest run never does, so
                # the assertion cannot stand.
                if served_role.get("role") == "active":
                    failures.append(
                        f"{TAMPER_PEER_REACTION} — GET /role answered "
                        "'active' across the whole down window"
                    )
            elif served_role.get("role") != "active":
                failures.append(
                    f"the field owner reports "
                    f"{served_role.get('role')!r} while its standby is "
                    "down — peer loss is not an event the controller of "
                    "record reacts to"
                )
                raise Abort
            if isinstance(owner.get("tick"), int) and isinstance(
                previous, int
            ) and owner["tick"] <= previous:
                failures.append(
                    f"the field owner's served tick stalled at "
                    f"{owner['tick']} over {previous} while its standby "
                    "was down — the owner kept scanning"
                )
                raise Abort
            previous = owner.get("tick")
        receipts_duty = pair.get(
            f"{duty_url}/receipts", "GET /receipts", failures
        )
        if not settled(receipts_duty, down_command):
            failures.append(
                f"the declared command {down_command['invoke']['command']}"
                f" on {down_command['invoke']['component']} never settled "
                "applied on the field owner during the down window — the "
                "standby's loss disturbed its command path"
            )
            raise Abort
        window_settled = True
        added = pair.get(f"{duty_url}/journal", "GET /journal",
                         failures)[len(pre_journal):]
        disturbed = [
            entry
            for entry in added
            if "role_changed" in entry.get("event", {})
            or "run_boundary" in entry.get("event", {})
        ]
        if disturbed:
            failures.append(
                f"the field owner's journal carries a role disturbance "
                f"across the standby's downtime: {disturbed}"
            )
            raise Abort
        file_records, file_boundaries = _role_entries(
            duty_files["journal_file"]
        )
        if file_records[entry_floor:]:
            failures.append(
                "the field owner's declared durable journal gained a "
                f"role record across the down window: "
                f"{file_records[entry_floor:]}"
            )
            raise Abort
        if len(file_boundaries) != boundary_floor:
            failures.append(
                f"the field owner's declared durable journal gained a "
                f"run boundary across the down window: {file_boundaries}"
            )
            raise Abort
        evidence["down_ticks"] = scans[-1]["tick"]
        digest_entries.append(
            {
                "phase": "down-window",
                "stopped": stopped,
                "scans": scans,
                "command": down_command["invoke"]["command"],
                "receipt": down_receipt,
                "settled": window_settled,
                "role_journal": 0,
            }
        )

        # Phase 4 — the standby's relaunch: it rejoins the pair on the
        # manifest's wiring and its declared persistence, and must then
        # reconverge to `tracking` inside the leg's declared window
        # while the field owner keeps writing. Nothing is driven on the
        # returning peer until its monitor answers, so the serve that
        # answers first is its cold-start one — the unsynchronized
        # verdict the fresh-peer promotion gate refuses.
        relaunched = tamper != "skip-restart"
        rejoined = None
        if relaunched:
            rig.standby, standby_url, preamble = pair.spawn_peer(
                args.controller,
                args.model,
                args.dt,
                rig.plant_addr,
                duty_url.removeprefix("http://"),
                standby_files,
                pair_token=pair.PAIR_TOKEN,
            )
            rig.standby_url = standby_url
            if standby_url is None:
                failures.append(
                    f"the relaunched standby exited at startup: "
                    f"{'; '.join(preamble[-2:]) or 'no diagnostic'}"
                )
                raise Abort
            rejoined = role(standby_url, failures)
            if rejoined.get("role") != "standby":
                failures.append(
                    f"the relaunched standby reports {rejoined} — "
                    "expected standby: it must rejoin as a tracker, "
                    "never claim the field"
                )
                raise Abort

        if not relaunched:
            # The doctored case: the return leg's own assertions held
            # against a peer never relaunched. The dead peer's monitor
            # answers nothing, which is the same observation the
            # reconvergence wait would make — named here so the negative
            # case carries the evidence the pair stage looks for.
            failures.append(
                "the returned standby never reconverged to tracking "
                f"inside the declared {RECONVERGE_SCANS}-tick window — "
                "its container was never relaunched, so no monitor "
                "answers"
            )
            raise Abort

        # The premature promote, fired at the returning monitor the
        # moment it first answers — before its first transfer completes.
        # Promotion requires a converged verdict, so the gate must
        # refuse it by name there; a monitor that already answers
        # `tracking` has nothing for the gate to hold and the request
        # succeeds through the documented switch instead, which the leg
        # reads as its own recorded disposition rather than a defect.
        gate = {"sync": sync_kind(rejoined or {}), "verdict": None,
                "switched": False}
        status, answer = pair.request(f"{standby_url}/promote", {})
        gate["verdict"] = (
            sorted(answer) if isinstance(answer, dict) else answer
        )
        if status == 200:
            gate["switched"] = True
        elif status != 409 or not (isinstance(answer, dict)
                                   and "not_converged" in answer):
            failures.append(
                f"POST /promote on the returning standby answered "
                f"{status} {answer} — the convergence gate must refuse "
                "it by name, never admit a promotion before the peer's "
                "first transfer completes"
            )
            raise Abort
        if tamper == "expect-open-gate":
            # The doctored expectation: the promotion gate is open, so
            # the request the leg fires before the peer's first
            # transfer completes is admitted. The honest run refuses it,
            # so the assertion cannot stand.
            if not gate["switched"]:
                failures.append(
                    f"{TAMPER_OPEN_GATE} — the promotion was refused "
                    f"with {gate['verdict']} before the returning "
                    "peer's first transfer completed"
                )
            raise Abort
        digest_entries.append({"phase": "gate", **gate})

        # Phase 5 — the reconvergence: tracking-first ticks until the
        # returned peer reports `tracking` inside the leg's declared
        # window, with the field owner's writes landing throughout. A
        # promotion the gate admitted has left the pair on the returned
        # peer, so the documented demote/promote order hands the field
        # back before the reconvergence is read.
        owner_url = duty_url
        tracker_url = standby_url
        if gate["switched"]:
            back = rig.switch(
                standby_url,
                duty_url,
                failures,
                demote_what="the promoted standby",
                promote_what="the field owner",
            )
            evidence["promoted_at"] = back["promote"]["tick"]
            digest_entries.append(
                {
                    "phase": "switch-back",
                    "demote": back["demote"],
                    "promote": back["promote"],
                    "ticks": back["ticks"],
                }
            )
        reconverged = None
        ticks = []
        for _ in range(RECONVERGE_SCANS):
            _tracked, owner = rig.tick(
                standby_url,
                duty_url,
                failures,
                diverged="the returned standby's image diverged from "
                "the field owner's at tick {tick} — its re-pull never "
                "realigned the pair",
            )
            ticks.append(owner["tick"])
            field_check(
                owner, plant_io, failures, "after the standby's return",
            )
            served_role = role(standby_url, failures)
            if tracking(served_role):
                reconverged = served_role
                break
        if reconverged is None:
            failures.append(
                "the returned standby never reconverged to tracking "
                f"inside the declared {RECONVERGE_SCANS}-tick window — "
                f"GET /role answers {served_role}"
            )
            raise Abort
        evidence["reconverged"] = ticks[-1]
        digest_entries.append(
            {
                "phase": "reconverged",
                "relaunched": relaunched,
                "ticks": ticks,
                "standby_role": reconverged,
            }
        )

        # Phase 6 — the restore: the pair back on its launch roles
        # through tracking-first ticks — identical images, the field
        # owner `active`, the declared standby `tracking` it. The pair
        # is already on those roles after the switch-back above, so this
        # is the settle that proves the return left no wedge.
        handover = []
        for _ in range(SETTLE_TICKS):
            _tracked, owner = rig.tick(
                standby_url,
                duty_url,
                failures,
                diverged="the restored pair's images diverged at tick "
                "{tick} — the standby's return left a wedge",
            )
            handover.append(owner["tick"])
        owner_role = role(duty_url, failures)
        standby_role = role(standby_url, failures)
        if owner_role.get("role") != "active":
            failures.append(
                f"the field owner reports {owner_role.get('role')!r} "
                "after the restore, expected active"
            )
            raise Abort
        if not tracking(standby_role):
            failures.append(
                f"the declared standby did not return to tracking "
                f"standby — GET /role answers {standby_role}"
            )
            raise Abort
        field = field_check(owner, plant_io, failures,
                            "closing the leg")
        evidence["final_tick"] = handover[-1]
        digest_entries.append(
            {
                "phase": "restore",
                "ticks": handover,
                "owner_role": owner_role.get("role"),
                "standby_role": standby_role.get("role"),
                "field": field,
            }
        )
    except Inconclusive:
        raise
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        if rig is not None:
            rig.close()
    return digest_entries, evidence, failures


def _try_role(url):
    """`GET /role` that answers None on transport error — the down
    window's own check that the stopped peer really is down."""
    try:
        return simulate.http(f"{url}/role")
    except Exception:
        return None


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
        choices=[
            "expect-applied",
            "expect-peer-reaction",
            "expect-open-gate",
            "skip-restart",
            "skip-down-window",
        ],
        help="doctor the leg's own expectation — the pass must fail "
        "naming the evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = standby_loss_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "standby-loss: the doctored expectation wanted a "
                "contract the pinned release does not carry — an "
                "inconclusive run offers it no evidence"
            )
            return 1
        eprint(f"standby-loss: inconclusive — {inconclusive}")
        print(f"standby-loss-digest inconclusive — {inconclusive}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"standby-loss: {line}")
        return 1
    for failure in failures:
        eprint(f"standby-loss: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"standby-loss: the {args.tamper} case passed silently "
                "— the leg never noticed the doctored expectation"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"standby-loss-digest {digest} — tracking by tick "
        f"{evidence['converged']}, the standby's write on point "
        f"{evidence['refused_on']} refused not_active, its loss "
        f"unnoticed through tick {evidence['down_ticks']}, tracking "
        f"again by tick {evidence['reconverged']}, run continued to "
        f"tick {evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())