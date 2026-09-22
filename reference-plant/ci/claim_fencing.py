#!/usr/bin/env python3
"""The standing field-claim lifecycle leg for the reference plant —
the consumer-side proof that the declared pair's spawned
`dcs-plant-server` enforces the single-writer fencing contract against
a third attachment while a pair peer owns the field (WW-ENG-003,
WW-OPS-003).

The pair leg (`ci/pair.py`) proves convergence, the receipted switch,
and the declared persistence — but no pair-stage leg attaches a third
plant-protocol client to the consumer plant, so the claim surface the
redundancy contract stands on went unexercised here. This leg mirrors
the platform lane's field-claim scenario on the customer-owned pair.
With the pair settled and a field-owning peer holding the plant's
writer claim — the launched active claims at startup on the current
release line, logging `field write-ownership claim held under owner
token N`; an older release claims only on promotion, so the leg first
runs the documented demote/promote switch to stand a claim up — a
dedicated third sim-net attachment exercises the standing claim's
whole lifecycle:

- mutation probes: the attachment's `write` and `step` answer the
  named fencing refusal — `write` carrying the point's io-fenced
  error, `step` the plant-level fenced — and the same mutations driven
  through the shipped `dcs-plant-ctl` exit nonzero naming the refusal,
  while the tool's unfenced `list`/`read` surfaces still answer;
- the field owner undisturbed: driven ticks keep landing the
  incumbent's writes and its served role stays `active`;
- the lifecycle verbs where the release speaks them (the owner-token
  claim line is their marker): a foreign token's `ensure_writer`
  refuses fenced — the conditional grant never preempts a standing
  owner — the owner's token answers `claimed_shared` and a write
  under the shared hold lands, `release_writer` drops only the
  caller's hold so the standing claim keeps fencing the released
  attachment and fresh probes, and a `release_writer` from a holder
  of nothing answers `done` without dissolving the claim;
- a rogue `claim_writer` resolving per the settled contract, never
  silently: a refusal leaves the claim and the owner untouched, and
  the unconditional preempt the contract grants must surface its
  supersession — on the current release the superseded owner journals
  `field_claim_lost` and demotes in place (degrade, never death), then
  the leg re-promotes it so the pair's launch roles stand unchanged.
  The preempt window also carries the state-vs-receipt audit: a
  receipted write on the model's writable internal `In` point
  admitted after the preempt but before the superseded owner's
  detection scan must settle `superseded` on both peers' receipt
  logs while every image — the demoted peer's, the tracking peer's
  adopted checkpoint, and the re-promoted line's — still reads the
  baseline; a superseded write that landed would contradict the
  journal both peers record;
  on the older claim-only release the superseded owner stays `active`
  but its scans start refusing on the fenced write — the disturbance
  the leg restores with the documented switch back. The dead-owner
  window — an owner disconnect leaving the claim standing — is the
  dead-owner leg's scope, not this leg's.

The attachment detaches cleanly — releasing whatever hold it still
carries where the surface speaks the verb, then closing — leaving the
pair's launch roles and the claim's owner unchanged for later legs.

Usage:

    claim_fencing.py --plant-server PATH --controller PATH \
        --plant-ctl PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `claim-fencing-digest <sha256>` line prints — the check
runs two passes and compares them (`claim-fencing-nondeterministic`).
A contract violation reports `claim-fencing: …` lines on stderr and
exits 1 — the check's `claim-fencing-failed`. The doctored cases prove
the leg's assertions fire rather than passing an unexercised contract:
`--tamper write-through` joins the probe attachment to the standing
claim so its mutations land, `--tamper foreign-ensure-granted`
requires the foreign conditional grant to be answered with a grant,
and `--tamper rogue-silent` requires the rogue claim to be refused —
each must fail the pass naming the evidence it saw.
"""

import argparse
import hashlib
import json
import subprocess
import sys

import failover
import force_carryover
import pair
import simulate
import takeover


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The driven scans the supersession watch and the demoted owner's
# reconvergence bound. The fencing loss demotes at the first fenced
# write and settles standby on the next quiesced scan; the demoted
# peer's tracking pull applies inside the same request, so the bounds
# are a handful of driven scans.
WATCH_SCANS = 4
RECONVERGE_SCANS = 6

# The claim tokens the leg's foreign and rogue probes assert — small
# fixed tokens that cannot collide with a controller's per-process
# minted token.
CLAIM_FOREIGN = 0xF00D
CLAIM_ROGUE = 0xF00E

# The substring the shipped tool's stderr carries for a fenced field
# mutation — the named refusal under the claim-wrapping tool line
# ("field mutation refused: another attachment owns field writes") and
# under a bare-write tool line's io-fenced error ("write fenced:
# another attachment owns field writes"). The same IoError display
# stands on both release surfaces.
FENCED_DETAIL = "another attachment owns field writes"

# The actor the fenced-boundary admission declares — the
# state-vs-receipt audit's submission, unique in both peers' receipt
# logs.
ACTOR = "ci-claim-fencing"


def mutation_fenced(verdict):
    """Whether a field-mutation answer is the named fencing refusal —
    `write` carries the point's io-fenced error nested under the `io`
    kind; `step` and the claim verbs carry the plant-level `fenced`
    kind."""
    error = (verdict or {}).get("error") or {}
    if error.get("kind") == "fenced":
        return True
    inner = error.get("error")
    return (
        error.get("kind") == "io"
        and isinstance(inner, dict)
        and "fenced" in inner
    )


def unsupported_verb(verdict):
    """Whether a claim-verb answer is the wire's `invalid_request` —
    a release whose plant server predates the lifecycle verbs."""
    error = (verdict or {}).get("error") or {}
    return error.get("kind") == "invalid_request"


def plant_ctl(tool, addr, *argv):
    """One shipped `dcs-plant-ctl` invocation against the pair's
    spawned plant — the census and the third attachment's mutation
    probes ride the released tool's surface. Returns `(exit, stdout,
    stderr)`."""
    run = subprocess.run(
        [tool, addr, *argv],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return run.returncode, run.stdout, run.stderr


def ctl_literal(value):
    """The `dcs-plant-ctl write` argument literal a stored sample value
    takes — `true|false`, an integer, or a float."""
    if "bool" in value:
        return "true" if value["bool"] else "false"
    if "int" in value:
        return str(value["int"])
    return repr(value["float"])


def foreign_step_probe(plant_addr):
    """One `step` probe through a fresh attachment — the standing
    claim's fencing verdict for a connection that never held it."""
    client = simulate.PlantClient(plant_addr)
    try:
        return client.request({"op": "step", "dt": 0})
    finally:
        client.close()


def tracking(report):
    """Whether a served RoleReport carries `standby` under the
    `tracking` sync state."""
    sync = report.get("sync") if isinstance(report, dict) else None
    return report.get("role") == "standby" and (
        isinstance(sync, dict) and "tracking" in sync
    )


def converged_sync(report):
    """Whether a served RoleReport carries `standby` under a converged
    sync state — `tracking`, or the `orphaned` verdict the
    ownership-stamped checkpoint contract reports while the tracked
    line's serving run holds no field claim. In this leg's restore
    window the rogue token owns the field and the successor's run
    serves checkpoints without one, so the demoted owner's honest
    report is `orphaned` — a promotable convergence — until its
    re-promotion preempts the rogue. A release line that predates the
    stamp reports `tracking` for the same convergence."""
    sync = report.get("sync") if isinstance(report, dict) else None
    return report.get("role") == "standby" and isinstance(
        sync, dict
    ) and ("tracking" in sync or "orphaned" in sync)


def claim_fencing_pass(args, tamper):
    """The claim-fencing run: converge (switching first where the
    release claims only on promotion), fenced probes, the claim
    lifecycle verbs the surface speaks, the rogue claim's settled
    answer, and the restore. Returns `(digest_entries, evidence,
    failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the claim-fencing "
            "leg has nothing to exercise"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = probe_io = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        plant_io = rig.plant_io
        # The dedicated third attachment the claim surface is
        # exercised through — the rig's own client stays read-only, so
        # every mutation verdict belongs to this one connection's
        # holds.
        probe_io = simulate.PlantClient(rig.plant_addr)
        with open(args.model) as handle:
            model = json.load(handle)
        points = failover.signal_points(model)
        if points is None:
            raise Abort(
                "the emitted model declares no p101-cmd/level-primary "
                "signal points — the leg has no field points to probe"
            )
        level, cmd = points["level"], points["cmd"]

        # Phase 1 — convergence. Where the launched active recorded
        # its startup claim line the owner token is legible and the
        # claim already stands; a release line that claims only on
        # promotion records no token — the documented switch then
        # stands a claim up on the promoted peer.
        converged = rig.converge(failures)
        owner = converged["owner"]
        owner_token = failover.owner_token(rig.duty_preamble)
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )
        owner_url, tracker_url = duty_url, standby_url
        if owner_token is None:
            switched = rig.switch(
                duty_url,
                standby_url,
                failures,
            )
            owner_url, tracker_url = standby_url, duty_url
            owner = switched["owner"]
            digest_entries.append(
                {
                    "phase": "switch",
                    "demote": switched["demote"],
                    "promote": switched["promote"],
                    "ticks": switched["ticks"],
                }
            )
        evidence["converged"] = converged["ticks"][-1]
        evidence["claim_surface"] = (
            "lifecycle" if owner_token is not None else "claim-only"
        )

        # Phase 2 — the fenced probes: the third attachment's write and
        # step answer the named fencing refusal, the shipped tool's
        # mutations exit nonzero naming it while its reads still
        # answer, and the field owner's own writes keep landing.
        level_value = failover.field_read(plant_io, level, failures)[
            "value"
        ]
        if tamper == "write-through":
            # The doctored attachment joins the standing claim before
            # probing — under the owner's token where the lifecycle
            # verbs stand, by an outright claim on the older surface —
            # so its mutations write through and the fenced-probe
            # assertions must fire.
            if owner_token is not None:
                probe_io.request(
                    {"op": "ensure_writer", "owner": owner_token}
                )
            else:
                probe_io.request(
                    {"op": "claim_writer", "owner": CLAIM_FOREIGN}
                )
        step_probe = probe_io.request({"op": "step", "dt": 0})
        write_probe = probe_io.request(
            {"op": "write", "point": level, "value": level_value}
        )
        if not mutation_fenced(step_probe):
            failures.append(
                "a third attachment's step was not refused fenced — "
                "the field held no enforceable writer claim: "
                f"{step_probe}"
            )
            raise Abort
        if not mutation_fenced(write_probe):
            failures.append(
                f"a third attachment's write was not refused fenced: "
                f"{write_probe}"
            )
            raise Abort
        ctl_list = plant_ctl(args.plant_ctl, rig.plant_addr, "list")
        ctl_read = plant_ctl(
            args.plant_ctl, rig.plant_addr, "read", str(level)
        )
        ctl_step = plant_ctl(args.plant_ctl, rig.plant_addr, "step", "0")
        ctl_write = plant_ctl(
            args.plant_ctl,
            rig.plant_addr,
            "write",
            str(level),
            ctl_literal(level_value),
        )
        if ctl_list[0] != 0 or '"points"' not in ctl_list[1]:
            failures.append(
                "dcs-plant-ctl list did not answer the field census — "
                f"exit {ctl_list[0]}: {ctl_list[2].strip()}"
            )
            raise Abort
        if ctl_read[0] != 0 or '"sample"' not in ctl_read[1]:
            failures.append(
                "dcs-plant-ctl read did not answer the point's "
                f"sample — exit {ctl_read[0]}: {ctl_read[2].strip()}"
            )
            raise Abort
        if ctl_step[0] == 0 or FENCED_DETAIL not in ctl_step[2]:
            failures.append(
                "a dcs-plant-ctl step was not refused with the named "
                f"fencing failure — exit {ctl_step[0]}: "
                f"{ctl_step[2].strip() or ctl_step[1].strip()}"
            )
            raise Abort
        if ctl_write[0] == 0 or FENCED_DETAIL not in ctl_write[2]:
            failures.append(
                "a dcs-plant-ctl write was not refused with the named "
                f"fencing failure — exit {ctl_write[0]}: "
                f"{ctl_write[2].strip() or ctl_write[1].strip()}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "probes",
                "step": failover.probe_kind(step_probe),
                "write": failover.probe_kind(write_probe),
                "ctl_step": ctl_step[0],
                "ctl_write": ctl_write[0],
            }
        )
        # The owner undisturbed: one driven tracking-first tick, then
        # the field still carries the owner's own write and its role
        # report still answers active.
        _tracked, owner = rig.tick(tracker_url, owner_url, failures)
        owner_role = pair.get(
            f"{owner_url}/role", "GET /role", failures
        )
        if owner_role.get("role") != "active":
            failures.append(
                "a fenced attachment's probes moved the field "
                f"owner's role — GET /role answers {owner_role}"
            )
            raise Abort
        held = failover.field_read(plant_io, cmd, failures)["value"]
        if held != simulate.snapshot_point(owner, cmd):
            failures.append(
                "the fenced probes disturbed the field owner — its "
                f"writes stopped landing: the field carries {held} "
                f"while its image reports "
                f"{simulate.snapshot_point(owner, cmd)}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "owner",
                "tick": owner["tick"],
                "field": held,
                "owner_role": owner_role,
            }
        )

        # Phase 3 — the lifecycle verbs on the third attachment. The
        # foreign-token ensure runs on every surface: `fenced` proves
        # the conditional grant never preempts a standing owner, while
        # `invalid_request` marks a release that predates the verbs —
        # the claim-only surface the remaining verbs cannot speak on.
        foreign_token = (
            owner_token if tamper == "foreign-ensure-granted"
            else CLAIM_FOREIGN
        )
        foreign = probe_io.request(
            {"op": "ensure_writer", "owner": foreign_token}
        )
        # The verb half needs both the wire verbs and a legible owner
        # token — a claim-only release answers `invalid_request`, and
        # a claim-on-promotion release records no token the shared
        # join could name.
        lifecycle = owner_token is not None and not unsupported_verb(
            foreign
        )
        if not mutation_fenced(foreign):
            # `invalid_request` marks a release whose plant predates
            # the verbs — the claim-only surface the remaining verbs
            # cannot speak on — unless the doctored leg substituted
            # the owner's token, where the grant or the unsupported
            # answer alike must fail the pass by name.
            if not (
                unsupported_verb(foreign)
                and tamper != "foreign-ensure-granted"
            ):
                failures.append(
                    "a foreign token's ensure_writer was not refused "
                    "fenced — the conditional grant preempted or "
                    f"joined a claim it must not reach: {foreign}"
                )
                raise Abort
        if lifecycle:
            shared = probe_io.request(
                {"op": "ensure_writer", "owner": owner_token}
            )
            if (
                shared.get("result") != "claimed_shared"
                or shared.get("owner") != owner_token
            ):
                failures.append(
                    "ensure_writer under the owner's token did not "
                    f"answer claimed_shared: {shared}"
                )
                raise Abort
            grant_write = probe_io.request(
                {"op": "write", "point": level, "value": level_value}
            )
            if grant_write.get("result") != "done":
                failures.append(
                    f"a write under the shared claim was refused: "
                    f"{grant_write}"
                )
                raise Abort
            release = probe_io.request({"op": "release_writer"})
            if release.get("result") != "done":
                failures.append(f"release_writer refused: {release}")
                raise Abort
            own_write = probe_io.request(
                {"op": "write", "point": level, "value": level_value}
            )
            if not mutation_fenced(own_write):
                failures.append(
                    "a released attachment's write was not fenced "
                    "back out — the release dropped more than the "
                    f"caller's hold: {own_write}"
                )
                raise Abort
            standing = foreign_step_probe(rig.plant_addr)
            if not mutation_fenced(standing):
                failures.append(
                    "the caller's release dropped the owner's claim "
                    f"— a fresh attachment's probe answered {standing}"
                )
                raise Abort
            idle = probe_io.request({"op": "release_writer"})
            if idle.get("result") != "done":
                failures.append(
                    "release_writer from a holder of nothing did not "
                    f"answer done: {idle}"
                )
                raise Abort
            still = foreign_step_probe(rig.plant_addr)
            if not mutation_fenced(still):
                failures.append(
                    "a harmless release changed the field — the "
                    f"claim dissolved: probes answered {still}"
                )
                raise Abort
            digest_entries.append(
                {
                    "phase": "lifecycle",
                    "foreign_ensure": failover.probe_kind(foreign),
                    "shared_ensure": shared.get("result"),
                    "grant_write": grant_write.get("result"),
                    "release": release.get("result"),
                    "own_write": failover.probe_kind(own_write),
                    "standing_probe": failover.probe_kind(standing),
                    "idle_release": idle.get("result"),
                    "still_probe": failover.probe_kind(still),
                }
            )
        else:
            digest_entries.append(
                {
                    "phase": "lifecycle",
                    "surface": "claim-only",
                    "foreign_ensure": failover.probe_kind(foreign),
                }
            )
        # The owner kept writing through the probes — one more driven
        # tick and the field still carries its image.
        _tracked, owner = rig.tick(tracker_url, owner_url, failures)
        held = failover.field_read(plant_io, cmd, failures)["value"]
        if held != simulate.snapshot_point(owner, cmd):
            failures.append(
                "the claim lifecycle disturbed the field owner — its "
                f"writes stopped landing: the field carries {held} "
                f"while its image reports "
                f"{simulate.snapshot_point(owner, cmd)}"
            )
            raise Abort

        # Phase 4 — the rogue claim: `claim_writer`'s grant is the
        # takeover surface, so the contract admits exactly two answers.
        # A refusal leaves the standing claim and the owner untouched;
        # the unconditional preempt must surface its supersession —
        # never a silent grant that leaves the owner writing into a
        # claim it no longer holds.
        journal0 = pair.get(
            f"{owner_url}/journal", "GET /journal", failures
        )
        rogue_token = (
            owner_token
            if tamper == "rogue-silent" and owner_token is not None
            else CLAIM_ROGUE
        )
        rogue = probe_io.request(
            {"op": "claim_writer", "owner": rogue_token}
        )
        if tamper == "rogue-silent":
            # The doctored expectation: the leg asserts the rogue claim
            # was refused — the contract's unconditional preempt, or a
            # shared join under the owner's token, must fail it naming
            # the actual answer.
            if rogue.get("result") != "error":
                failures.append(
                    f"the rogue claim answered {rogue} — the doctored "
                    "leg required the claim refused"
                )
                raise Abort
        if rogue.get("result") == "error":
            probe = foreign_step_probe(rig.plant_addr)
            role = pair.get(f"{owner_url}/role", "GET /role", failures)
            if not mutation_fenced(probe):
                failures.append(
                    "the refused rogue claim still dropped the "
                    f"standing claim: {probe}"
                )
                raise Abort
            if role.get("role") != "active":
                failures.append(
                    "a refused rogue claim moved the field owner's "
                    f"role: {role}"
                )
                raise Abort
            digest_entries.append(
                {
                    "phase": "rogue",
                    "answer": "refused",
                    "probe": failover.probe_kind(probe),
                    "owner_role": role,
                }
            )
            evidence["rogue"] = "refused"
        elif rogue.get("result") != "done":
            failures.append(
                f"the rogue claim answered {rogue} — neither the "
                "refusal nor the unconditional preempt the contract "
                "admits"
            )
            raise Abort
        elif lifecycle:
            # Preempted on the lifecycle surface: the claim names the
            # rogue token now, and the superseded owner's first fenced
            # scan must journal `field_claim_lost` and demote it in
            # place — the monitor answering throughout, because a
            # degrade is not a death.
            #
            # The preempt window also admits a receipted command: a
            # write on the model's writable internal `In` point
            # submitted after the preempt lands but before the
            # superseded owner's detection scan. That scan applies
            # the admission at its command boundary, then fences on
            # its field write and demotes — the superseded receipt is
            # the settle both journals must carry, and the state-vs-
            # receipt audit the contract demands: a rejected
            # superseded write never lands, so the demoted image and
            # every checkpoint it serves must read the baseline.
            internal = force_carryover.force_target(model)
            admission = baseline = None
            if internal is not None:
                baseline = simulate.snapshot_point(owner, internal)
                if baseline is None or "bool" not in baseline:
                    failures.append(
                        f"the internal write target point {internal} "
                        f"serves {baseline} — a bool baseline the leg "
                        "can flip is required"
                    )
                    raise Abort
                admission = takeover.write_value(
                    internal, not baseline["bool"]
                )
                status, receipt = pair.request(
                    f"{owner_url}/command",
                    {"command": admission, "actor": ACTOR},
                )
                if (
                    status != 200
                    or simulate.receipt_outcome(receipt) != "accepted"
                ):
                    failures.append(
                        f"the preempt-window write {admission} "
                        f"answered {status} {receipt}, expected an "
                        "accepted receipt"
                    )
                    raise Abort
            roles = []
            settled = None
            adopted = None
            for _ in range(WATCH_SCANS):
                pair.scan(owner_url, failures)
                report = pair.get(
                    f"{owner_url}/role", "GET /role", failures
                )
                roles.append(report.get("role"))
                if (
                    admission is not None
                    and adopted is None
                    and report.get("role") == "demoting"
                ):
                    # The detection scan just fenced the boundary —
                    # the demoted peer now serves its superseded
                    # image. Drive one tracking scan so the standby's
                    # pull adopts that checkpoint before the demoted
                    # peer's own pull can overwrite its image with
                    # the standby's older one: whatever value the
                    # fenced boundary left, this adoption is the hop
                    # the QA finding rode to the surviving line.
                    adopted = pair.scan(tracker_url, failures)
                if report.get("role") == "standby":
                    settled = report
                    break
            journal1 = pair.get(
                f"{owner_url}/journal", "GET /journal", failures
            )
            added = journal1[len(journal0) :]
            if settled is None:
                failures.append(
                    "the superseded owner never demoted — the rogue "
                    "claim moved the claim but the role stayed "
                    f"{roles}"
                )
                raise Abort
            losses = [
                entry["event"]["field_claim_lost"]
                for entry in added
                if "field_claim_lost" in entry.get("event", {})
            ]
            if not losses:
                failures.append(
                    "the preemption was silent — the superseded "
                    "owner's journal recorded no field_claim_lost"
                )
                raise Abort
            transitions = [
                (frm, to)
                for _tick, frm, to in pair.role_transitions(added)
            ]
            if transitions != [
                ("active", "demoting"),
                ("demoting", "standby"),
            ]:
                failures.append(
                    "the superseded owner's role walk is "
                    f"{transitions}, expected active → demoting → "
                    "standby"
                )
                raise Abort
            # The durable journal the manifest declares carries the
            # same evidence — the persistence half of the preemption
            # record.
            journal_path = (
                rig.duty_files.get("journal_file")
                if owner_url == duty_url
                else rig.standby_files.get("journal_file")
            )
            if journal_path is not None:
                records = pair.journal_records(journal_path)
                durable = [
                    record_
                    for kind, record_ in records
                    if kind == "entry"
                ]
                if not any(
                    "field_claim_lost" in entry.get("event", {})
                    for entry in durable
                ):
                    failures.append(
                        "the superseded owner's durable journal file "
                        "recorded no field_claim_lost — the preemption "
                        "evidence never persisted"
                    )
                    raise Abort
            # The state-vs-receipt audit: the admission's receipt
            # settles `superseded` on both peers' logs — and both
            # peers' images read the baseline, the adopted checkpoint
            # included. A superseded write that landed would read the
            # flipped value on the surviving line while the journals
            # claim it never applied.
            outcomes = {}
            fenced_images = {}
            if admission is not None:
                receipts = {
                    name: pair.get(f"{url}/receipts", "GET /receipts", failures)
                    for name, url in (
                        ("owner", owner_url),
                        ("tracker", tracker_url),
                    )
                }
                for name, log in receipts.items():
                    entries = [
                        entry
                        for entry in log
                        if entry.get("command") == admission
                        and entry.get("actor") == ACTOR
                    ]
                    if len(entries) != 1:
                        failures.append(
                            f"the {name} peer's receipt log carries "
                            f"{len(entries)} entries for the preempt-"
                            "window admission — the superseded settle "
                            "must land exactly once on both peers"
                        )
                        continue
                    outcomes[name] = simulate.receipt_outcome(entries[0])
                    if outcomes[name] != "superseded":
                        failures.append(
                            f"the {name} peer settled the admission "
                            f"{outcomes[name]} — the fenced boundary "
                            "must settle it rejected superseded"
                        )
                if adopted is not None:
                    fenced_images["tracker_adopted"] = (
                        simulate.snapshot_point(adopted, internal)
                    )
                    if fenced_images["tracker_adopted"] != baseline:
                        failures.append(
                            "the tracking peer adopted "
                            f"{fenced_images['tracker_adopted']} for "
                            f"point {internal} from the fenced "
                            f"checkpoint, expected the baseline "
                            f"{baseline} — the superseded write "
                            "propagated through the quiesced image"
                        )
                for name, url in (
                    ("owner", owner_url),
                    ("tracker", tracker_url),
                ):
                    snapshot = pair.get(
                        f"{url}/snapshot", "GET /snapshot", failures
                    )
                    fenced_images[name] = simulate.snapshot_point(
                        snapshot, internal
                    )
                    if fenced_images[name] != baseline:
                        failures.append(
                            f"the {name} peer's image reads "
                            f"{fenced_images[name]} for point "
                            f"{internal}, expected the baseline "
                            f"{baseline} — a superseded write landed "
                            "on the line the receipt says rejected it"
                        )
            digest_entries.append(
                {
                    "phase": "rogue",
                    "answer": "preempted",
                    "watch": roles,
                    "claim_losses": losses,
                    "transitions": transitions,
                    "admission": admission,
                    "settled_outcomes": outcomes,
                    "fenced_images": fenced_images,
                }
            )
            evidence["rogue"] = "preempted"

            # The restore: the demoted owner reconverges on the
            # monitor address its peer's pulls announced — `orphaned`
            # while the rogue's claim stands (the tracked line's
            # serving run owns nothing, the named verdict the wedge
            # fix reports instead of healthy tracking), `tracking` on
            # a stamp-less release line — then re-promotes: the
            # promotion claim preempts the rogue token, so the field
            # stays claimed throughout.
            report = None
            for _ in range(RECONVERGE_SCANS):
                report = pair.get(
                    f"{owner_url}/role", "GET /role", failures
                )
                if converged_sync(report):
                    break
                pair.scan(owner_url, failures)
            else:
                failures.append(
                    "the demoted owner never reconverged on "
                    f"its announced successor — GET /role answers "
                    f"{report}"
                )
                raise Abort
            promoted = rig.promote(
                owner_url, failures, what="the demoted field owner"
            )
            handover = []
            for _ in range(pair.HANDOVER_TICKS):
                _tracked, owner = rig.tick(
                    tracker_url,
                    owner_url,
                    failures,
                    diverged="the restored pair's images diverged at "
                    "tick {tick} — the re-promotion was not bumpless",
                )
                handover.append(owner["tick"])
            owner_role = pair.get(
                f"{owner_url}/role", "GET /role", failures
            )
            tracker_role = pair.get(
                f"{tracker_url}/role", "GET /role", failures
            )
            if owner_role.get("role") != "active":
                failures.append(
                    "the re-promoted owner never settled active — "
                    f"GET /role answers {owner_role}"
                )
                raise Abort
            if not tracking(tracker_role):
                failures.append(
                    "the tracking peer did not return to standby — "
                    f"GET /role answers {tracker_role}"
                )
                raise Abort
            # The claim's owner token is the field owner's own again —
            # a foreign probe stays fenced, the owner-token join still
            # answers claimed_shared, and the owner's writes land.
            post_step = foreign_step_probe(rig.plant_addr)
            if not mutation_fenced(post_step):
                failures.append(
                    "the restored claim does not fence foreign "
                    f"probes: {post_step}"
                )
                raise Abort
            post_shared = probe_io.request(
                {"op": "ensure_writer", "owner": owner_token}
            )
            if (
                post_shared.get("result") != "claimed_shared"
                or post_shared.get("owner") != owner_token
            ):
                failures.append(
                    "the restored claim does not name the field "
                    "owner's token — ensure_writer answered "
                    f"{post_shared}"
                )
                raise Abort
            post_release = probe_io.request({"op": "release_writer"})
            if post_release.get("result") != "done":
                failures.append(
                    "the restore probe's release_writer refused: "
                    f"{post_release}"
                )
                raise Abort
            held = failover.field_read(plant_io, cmd, failures)["value"]
            if held != simulate.snapshot_point(owner, cmd):
                failures.append(
                    "the restored field owner is not writing — the "
                    f"field carries {held} while its image reports "
                    f"{simulate.snapshot_point(owner, cmd)}"
                )
                raise Abort
            if admission is not None:
                promoted_value = simulate.snapshot_point(
                    owner, internal
                )
                if promoted_value != baseline:
                    failures.append(
                        f"the re-promoted line carries "
                        f"{promoted_value} for point {internal}, "
                        f"expected the baseline {baseline} — the "
                        "superseded write survived the restore"
                    )
            digest_entries.append(
                {
                    "phase": "restore",
                    "promote": promoted,
                    "ticks": handover,
                    "owner_role": owner_role,
                    "tracker_role": tracker_role,
                    "post_probe": failover.probe_kind(post_step),
                    "post_shared": post_shared.get("result"),
                }
            )
            evidence["restored_at"] = promoted["tick"]
        else:
            # Preempted on the claim-only surface: the superseded
            # owner keeps serving `active` — the release predates the
            # fencing-loss demotion — but its scans start refusing on
            # the fenced write: the preempt is never silent, the
            # field's claim moved off its name. The documented switch
            # then restores the launch roles: demote the superseded
            # peer, promote the tracker — whose promotion claim
            # preempts the rogue token — and hand the field back.
            role = pair.get(f"{owner_url}/role", "GET /role", failures)
            if role.get("role") != "active":
                failures.append(
                    "the claim-only superseded owner left role "
                    f"active — GET /role answers {role}"
                )
                raise Abort
            status, body = pair.request(
                f"{owner_url}/scan", {"scans": 1}
            )
            if status == 200 or "fenc" not in str(body):
                failures.append(
                    "the rogue claim preempted silently — the "
                    "superseded owner's scan still answers "
                    f"{status} {body}"
                )
                raise Abort
            digest_entries.append(
                {
                    "phase": "rogue",
                    "answer": "preempted",
                    "owner_role": role,
                    "superseded_scan": status,
                }
            )
            evidence["rogue"] = "preempted"
            restored = rig.switch(
                owner_url,
                tracker_url,
                failures,
                demote_what="the superseded field owner",
                promote_what="the tracking peer",
            )
            post_step = foreign_step_probe(rig.plant_addr)
            if not mutation_fenced(post_step):
                failures.append(
                    "the restored claim does not fence foreign "
                    f"probes: {post_step}"
                )
                raise Abort
            owner = restored["owner"]
            held = failover.field_read(plant_io, cmd, failures)["value"]
            if held != simulate.snapshot_point(owner, cmd):
                failures.append(
                    "the restored field owner is not writing — the "
                    f"field carries {held} while its image reports "
                    f"{simulate.snapshot_point(owner, cmd)}"
                )
                raise Abort
            digest_entries.append(
                {
                    "phase": "restore",
                    "demote": restored["demote"],
                    "promote": restored["promote"],
                    "ticks": restored["ticks"],
                    "post_probe": failover.probe_kind(post_step),
                }
            )
            evidence["restored_at"] = restored["promote"]["tick"]

        evidence["final_tick"] = owner["tick"]
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        if probe_io is not None:
            # Detach cleanly: release whatever hold this attachment
            # still carries — a hold left standing would keep the
            # field claimed for a dead token — then close. The release
            # drops only this connection's hold, so it can never take
            # the owner's claim down with it; a surface that predates
            # the verb just errors the request.
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
    parser.add_argument(
        "--plant-ctl",
        required=True,
        help="the released dcs-plant-ctl binary — the shipped surface "
        "the third attachment's mutation probes ride",
    )
    parser.add_argument("--model", required=True)
    parser.add_argument("--dynamics", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument(
        "--tamper",
        choices=[
            "write-through",
            "foreign-ensure-granted",
            "rogue-silent",
        ],
        help="doctor the run so a forbidden condition occurs — the "
        "pass must fail naming the evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = claim_fencing_pass(
            args, args.tamper
        )
    except Abort as abort:
        for line in abort.args:
            eprint(f"claim-fencing: {line}")
        return 1
    for failure in failures:
        eprint(f"claim-fencing: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"claim-fencing: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored claim"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"claim-fencing-digest {digest} — tracking by tick "
        f"{evidence['converged']}, claim surface "
        f"{evidence['claim_surface']}, the rogue claim "
        f"{evidence['rogue']}, run ended at tick "
        f"{evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
