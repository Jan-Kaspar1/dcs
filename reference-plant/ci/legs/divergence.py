#!/usr/bin/env python3
"""The staged-vs-field divergence leg for the reference plant — the
consumer-side proof that the deployed redundant pair answers the
promotion of a stale standby honestly: the failover-integrity
clause's divergence half on the customer pair (WW-ENG-003,
WW-LCM-001).

The pair leg (`ci/legs/pair.py`) proves the receipted demote/promote
switch and the refusal leg (`ci/legs/refusal.py`) the pre-transfer and
role-gate refusals; this leg proves the remaining promotion gate —
a tracking standby whose staged field `Out` image no longer matches
the field it would take over, the misconfiguration a customer
driving their own pair produces when the standby's checkpoint path
partitions while the field moves and the stale peer is then
promoted. The rig reads the standby wiring and persistence fields
out of `deploy/manifest.json` and spawns the released tooling
exactly as the sibling legs do. The run's first half:

- converges the declared standby to `tracking` through the pair
  leg's driven-tick loop — the peers resting at one tick with
  identical images, the field owner's writes landed, both adopted
  receipt logs agreeing;
- withholds the tracking standby's checkpoint pulls for an
  observation window: no `POST /scan` reaches either peer — the
  driven run makes the partition literal — while a field-side write
  lands through the run's dedicated plant-protocol client, the same
  connection the simulate stage's `inject_fault`/`clear_fault` ops
  use. The client joins the field's standing writer claim under the
  duty's recorded owner token — the conditional grant the failover
  leg's fencing probes prove — writes the carried `p101-cmd` output
  the standby's staged image covers, and hands the hold back, so the
  standby's staged copy no longer matches field reality;
- resumes the pull path — the next `POST /scan`'s transfer applies
  at the staged image's own tick, so the same-tick comparison reads
  the field the image describes: the served `GET /role` must report
  `standby` under the `diverged` sync state naming the perturbed
  output with both sides' values, and the standby's served journal —
  and its durable `--journal-file` — must carry the
  `divergence_detected` record attributed to the compared tick;
- posts `POST /promote` against the stale peer: the answer must be
  the named `409 not_converged` refusal carrying the diverged sync
  report — never a silent promote, never a `promoting` — with no
  field hand-off: the duty stays `active`, the stale peer stays
  `standby` and diverged, no `role_changed` lands on either peer's
  journal, the injected write stands un-overwritten by any stale
  staged image, and the duty's settled receipts are untouched;
- proves the active undisturbed: the duty's next driven scan's write
  lands — the field healing back to its image — and the standby's
  next same-tick comparison matches again, the verdict resolving to
  `tracking` with its `divergence_resolved` record journaled and
  the pair's driven lockstep restored.

The control half runs a fresh pair through the identical window
with no field-side write: the resumed pull's comparison matches the
untouched field, the standby stays `tracking`, and the documented
`demote`/`promote` switch succeeds — the refusal names the
staged-vs-field divergence, not the partition's staleness. The
launch roles then restore.

Usage:

    divergence.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `divergence-digest <sha256>` line prints — the check
runs two passes and compares them (`divergence-nondeterministic`). A
contract violation reports `divergence: …` lines on stderr and exits
1 — the check's `divergence-missed`. `--tamper skip-field-write`
doctors the run — the field-side write skipped while the diverged
report and the refused promote are still asserted — so the check
proves the leg reports `divergence-missed` rather than passing
silently.
"""

import argparse
import hashlib
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import failover
import pair
import refusal
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored case: the field-side write skipped while the leg
# still asserts the diverged report and the refused promote
# must surface the named diagnostic — never a silently
# unconvinced pass.
LEG = {
    "order": 150,
    "title": "the staged-vs-field divergence leg",
    "passes": "divergence-leg",
    "failed": "divergence-missed",
    "tampers": [
        {
            "name": "skip-field-write",
            "passed": "a skipped field-side write passed the divergence leg",
            "missed": "the skip-field-write case did not report its named diagnostic",
            "evidence": ["expected the diverged report"],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The carried field `Out` signal the window's write perturbs —
# resolved out of the emitted model's signal index, never
# hard-coded. The standby's staged image covers it because the pump
# group's delivered command writes it every scan.
CARRIED_SIGNAL = "p101-cmd"

# The writer-claim token the leg's field-side write joins under when
# the launched duty recorded none — an unclaimed field fails closed,
# so the write still needs a claim; the hold releases right after
# the write lands, restoring the unclaimed state it found.
LEG_OWNER_TOKEN = 0x488


def carried_point(model):
    """The point the emitted model's signal index binds the carried
    `p101-cmd` output to — lowest-signal-id-wins, the served index's
    resolution rule — asserted a declared boolean `out` point so the
    leg perturbs a field-carried staged-image entry, not an internal
    or mistyped one."""
    bound = None
    for signal in model.get("signals", []):
        if signal.get("name") == CARRIED_SIGNAL and (
            bound is None or signal["id"] < bound[0]
        ):
            bound = (signal["id"], signal["source"])
    if bound is None:
        return None
    point = bound[1]
    for entry in model.get("io_points", []):
        if entry.get("id") == point:
            declared = (
                entry.get("direction") == "out"
                and entry.get("value_type") == "bool"
                and entry.get("channel") is not None
            )
            return point if declared else None
    return None


def diverged_mismatches(report):
    """The mismatch list a served RoleReport's `diverged` sync state
    carries — None when the report is not diverged."""
    sync = report.get("sync") if isinstance(report, dict) else None
    if isinstance(sync, dict):
        diverged = sync.get("diverged")
        if isinstance(diverged, dict):
            return diverged.get("mismatches")
    return None


def journal_events(entries, name):
    """The `(tick, payload)` stream one journal event kind carries
    through a served-or-durable entry list — the standby's own
    divergence records."""
    return [
        (entry["tick"], entry["event"][name])
        for entry in entries
        if name in entry.get("event", {})
    ]


def field_side_write(plant_io, point, value, owner, failures):
    """The window's field-side write through the run's dedicated
    plant-protocol client: the client joins the field's standing
    writer claim under the recorded owner token — the same
    conditional grant the failover leg's fencing probes exercise —
    writes, reads the landed value back, and releases its hold so
    the claim stands exactly as found. Returns `(join, landed)` —
    the grant's claim-state word and the stored value."""
    join = failover.probe_kind(
        plant_io.request({"op": "ensure_writer", "owner": owner})
    )
    if join != "granted":
        failures.append(
            f"the field-side write's claim join under the recorded "
            f"owner token answered {join} — the leg could not reach "
            "the field it perturbs"
        )
        raise Abort
    write = plant_io.request(
        {"op": "write", "point": point, "value": value}
    )
    if write.get("result") != "done":
        failures.append(
            f"the field-side write on point {point} answered {write}"
        )
        raise Abort
    landed = failover.field_read(plant_io, point, failures).get("value")
    plant_io.request({"op": "release_writer"})
    return join, landed


def moved_field_points(samples, image):
    """The field `Out` points whose plant-stored value diverges from
    the owner's served image — the evidence a stale staged image
    landed, or the owner's writes stopped, per point."""
    return [
        point
        for point, sample in sorted(samples.items())
        if (sample or {}).get("value")
        != (image.get(point) or {}).get("value")
    ]


def diverged_run(args, declared, point, tamper, failures):
    """The divergence half: converge, withhold the standby's pulls
    through the field-side-write window, resume, and assert the
    stale peer reports `diverged` naming the perturbed output and
    its promote is refused `not_converged` with no hand-off.
    Returns `(digest_entries, evidence)`."""
    digest_entries, evidence = [], {}
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        plant_io = rig.plant_io

        # Phase 1 — convergence and the settled baseline: the peers
        # rest at one tick with identical images, the field holds the
        # duty's writes, and both peers' adopted receipt logs agree.
        converged = rig.converge(failures)
        owner = converged["owner"]
        converge_tick = converged["ticks"][-1]
        evidence["converged"] = converge_tick
        staged_value = simulate.snapshot_point(owner, point)
        if not isinstance(staged_value, dict) or "bool" not in staged_value:
            failures.append(
                f"the carried point {point} serves {staged_value} in "
                "the owner's image — the leg perturbs a boolean "
                "field output"
            )
            raise Abort
        baseline_receipts = pair.get(
            f"{duty_url}/receipts", "GET /receipts", failures
        )
        if baseline_receipts != pair.get(
            f"{standby_url}/receipts", "GET /receipts", failures
        ):
            failures.append(
                "the peers' receipt logs diverged before the window — "
                "the pair was not settled"
            )
            raise Abort
        image = {
            entry["point"]: entry.get("sample")
            for entry in owner.get("points", [])
            if entry.get("direction") == "out"
        }
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )

        # Phase 2 — the observation window: no scan reaches either
        # peer, so the standby's checkpoint pulls stand withheld and
        # the field is still except the field-side write the run's
        # plant-protocol client lands on the carried output — the
        # claim joined under the duty's recorded owner token, the hold
        # handed back after the write. Under --tamper
        # skip-field-write the write never lands while every later
        # assertion still stands — the leg must fail it.
        injected = {"bool": not staged_value["bool"]}
        window = {
            "staged": staged_value,
            "injected": injected,
            "standby_role": pair.get(
                f"{standby_url}/role", "GET /role", failures
            ),
            "duty_role": pair.get(
                f"{duty_url}/role", "GET /role", failures
            ),
        }
        if tamper == "skip-field-write":
            window["write"] = "skipped"
        else:
            owner_token = failover.owner_token(rig.duty_preamble)
            join, landed = field_side_write(
                plant_io,
                point,
                injected,
                owner_token
                if owner_token is not None
                else LEG_OWNER_TOKEN,
                failures,
            )
            if landed != injected:
                failures.append(
                    f"the field-side write left point {point} at "
                    f"{landed}, expected {injected} — the write never "
                    "reached the field the leg perturbs"
                )
                raise Abort
            window.update({"join": join, "landed": landed})
        digest_entries.append({"phase": "window", **window})

        # Phase 3 — the resumed pull path: the transfer applies at the
        # staged image's own tick, so its same-tick field comparison
        # runs — the served role must report standby under `diverged`
        # naming the perturbed output with both sides' values, and the
        # standby's journal must carry the divergence_detected record
        # attributed to the compared tick.
        pair.scan(standby_url, failures)
        report = pair.get(f"{standby_url}/role", "GET /role", failures)
        mismatches = diverged_mismatches(report)
        want = [
            {"point": point, "staged": staged_value, "field": injected}
        ]
        if report.get("role") != "standby" or mismatches != want:
            failures.append(
                f"the resumed checkpoint pull left the standby "
                f"reporting {report} — expected the diverged report "
                f"naming point {point} with staged {staged_value} "
                f"against field {injected}"
            )
            raise Abort
        journal = pair.get(
            f"{standby_url}/journal", "GET /journal", failures
        )
        detections = journal_events(journal, "divergence_detected")
        if detections != [(converge_tick, {"mismatches": want})]:
            failures.append(
                f"the standby's journal carries the divergence "
                f"records {detections} — expected one "
                f"divergence_detected at tick {converge_tick} naming "
                f"point {point}"
            )
            raise Abort
        evidence["diverged_at"] = report["tick"]
        digest_entries.append(
            {
                "phase": "diverged",
                "report": report,
                "detected": detections,
            }
        )

        # Phase 4 — the refused promote: the stale peer must answer
        # the named not_converged carrying the diverged report, with
        # no field hand-off — the duty stays active, the peer stays
        # standby and diverged, no role_changed lands on either
        # journal, the injected write stands un-overwritten, and the
        # settled receipts are untouched.
        status, refused = pair.request(f"{standby_url}/promote", {})
        refusal_sync = (
            refused.get("not_converged", {}).get("sync")
            if isinstance(refused, dict)
            else None
        )
        if status != 409 or (
            not isinstance(refusal_sync, dict)
            or diverged_mismatches({"sync": refusal_sync}) != want
        ):
            failures.append(
                f"POST /promote on the stale standby answered {status} "
                f"{refused} — expected the 409 not_converged refusal "
                f"carrying the diverged report naming point {point}"
            )
            raise Abort
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        standby_after = pair.get(
            f"{standby_url}/role", "GET /role", failures
        )
        if duty_role.get("role") != "active":
            failures.append(
                f"the field owner reports {duty_role} after the "
                "refused promote — the refusal handed the field off"
            )
        if standby_after != report:
            failures.append(
                f"the refused promote changed the standby's report "
                f"to {standby_after} from {report}"
            )
        samples = refusal.field_out_samples(plant_io)
        moved = moved_field_points(samples, image)
        if moved != [point]:
            failures.append(
                f"the field diverges from the active's image at "
                f"{moved} — expected the injected point {point} "
                "alone; anything else is a stale staged image's "
                "write or a disturbed owner"
            )
        for name, url in (
            (rig.duty_decl["name"], duty_url),
            (rig.standby_decl["name"], standby_url),
        ):
            transitions = rig.served_transitions(url, failures)
            if transitions:
                failures.append(
                    f"{name}'s journal carries the role transitions "
                    f"{transitions} — a field hand-off occurred"
                )
        if (
            pair.get(f"{duty_url}/receipts", "GET /receipts", failures)
            != baseline_receipts
        ):
            failures.append(
                "the active's receipt log moved across the refused "
                "promote — the settled audit is not undisturbed"
            )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "refused",
                "status": status,
                "refused": refused,
                "duty_role": duty_role,
                "standby_role": standby_after,
                "moved": moved,
            }
        )

        # Phase 5 — undisturbed and resolved: the duty's next driven
        # scan's write lands — the field healing back to its image —
        # the standby's next same-tick comparison matching again with
        # its divergence_resolved record journaled, and the pair's
        # driven lockstep restored.
        duty_scan = pair.scan(duty_url, failures)
        landed = failover.field_read(plant_io, point, failures).get(
            "value"
        )
        if landed != simulate.snapshot_point(duty_scan, point):
            failures.append(
                f"the active's scan left point {point} at {landed} "
                f"while its image reports "
                f"{simulate.snapshot_point(duty_scan, point)} — the "
                "owner's writes stopped landing"
            )
            raise Abort
        pair.scan(standby_url, failures)
        resolved = pair.get(f"{standby_url}/role", "GET /role", failures)
        sync = resolved.get("sync")
        if resolved.get("role") != "standby" or not (
            isinstance(sync, dict) and "tracking" in sync
        ):
            failures.append(
                "the standby never resolved its divergence once the "
                f"field matched again — GET /role answers {resolved}"
            )
            raise Abort
        journal = pair.get(
            f"{standby_url}/journal", "GET /journal", failures
        )
        resolutions = journal_events(journal, "divergence_resolved")
        if len(resolutions) != 1 or resolutions[0][0] != converge_tick + 1:
            failures.append(
                f"the standby's journal carries the resolutions "
                f"{resolutions} — expected one divergence_resolved at "
                f"tick {converge_tick + 1}"
            )
            raise Abort
        compared = resolutions[0][1].get("compared", [])
        cleared = [entry for entry in compared if entry.get("point") == point]
        if (
            len(cleared) != 1
            or cleared[0].get("staged") != cleared[0].get("field")
            or not all(
                entry.get("staged") == entry.get("field")
                for entry in compared
            )
        ):
            failures.append(
                f"the divergence_resolved record's compared evidence "
                f"{resolutions[0][1]} — expected every staged point "
                f"matched, point {point} included"
            )
            raise Abort
        evidence["resolved_at"] = resolutions[0][0]
        # The duty catches the standby's extra scan, then the pair's
        # tracking-first ticks prove the restored lockstep — the
        # standby's per-scan comparison matching the field the owner
        # keeps writing.
        pair.scan(duty_url, failures)
        healed = rig.converge(failures, count=2)
        # The durable record: the standby's declared --journal-file
        # carries the divergence pair in seq order beside the served
        # record's, and the field stands the owner's image again.
        journal_path = rig.standby_files.get("journal_file")
        if journal_path is None or not os.path.exists(journal_path):
            failures.append(
                "the standby's declared journal file does not exist — "
                "the --journal-file flag was not honored"
            )
            raise Abort
        records = pair.journal_records(journal_path)
        entries = [
            record for kind, record in records if kind == "entry"
        ]
        seqs = [entry["seq"] for entry in entries]
        if seqs != list(range(1, len(seqs) + 1)):
            failures.append(
                f"the standby's journal seqs are not 1..n in order: "
                f"{seqs}"
            )
        if journal_events(entries, "divergence_detected") != detections:
            failures.append(
                "the standby's durable journal diverges from its "
                "served divergence_detected record — the monitor "
                "does not answer the record it persists"
            )
        if journal_events(entries, "divergence_resolved") != resolutions:
            failures.append(
                "the standby's durable journal diverges from its "
                "served divergence_resolved record"
            )
        duty_journal = pair.get(
            f"{duty_url}/journal", "GET /journal", failures
        )
        if journal_events(
            duty_journal, "divergence_detected"
        ) or journal_events(duty_journal, "divergence_resolved"):
            failures.append(
                "the field owner's journal carries divergence "
                "records — a standby-local verdict leaked onto the "
                "active's audit"
            )
        if (
            pair.get(f"{duty_url}/receipts", "GET /receipts", failures)
            != baseline_receipts
        ):
            failures.append(
                "the active's receipt log moved across the run — "
                "the settled audit is not undisturbed"
            )
        field = refusal.field_out_samples(plant_io)
        for mismatch in refusal.field_mismatches(healed["owner"], field):
            failures.append(
                f"{mismatch} after the restore — the field does not "
                "hold the active's writes"
            )
        if failures:
            raise Abort
        evidence["final_tick"] = healed["owner"]["tick"]
        digest_entries.append(
            {
                "phase": "restored",
                "resolved": resolved,
                "resolution": {
                    "tick": resolutions[0][0],
                    "compared": len(compared),
                    "cleared": cleared,
                },
                "healed_ticks": healed["ticks"],
                "duty_role": healed["duty_role"],
                "standby_role": healed["standby_role"],
                "journal_records": len(records),
            }
        )
    finally:
        if rig is not None:
            rig.close()
    return digest_entries, evidence


def control_run(args, declared, failures):
    """The control half: the identical withheld-pull window with no
    field-side write — the standby reconverges tracking and the
    documented switch promotes it, proving the refusal names the
    staged-vs-field divergence rather than the partition's
    staleness. Returns `(digest_entries, evidence)`."""
    digest_entries, evidence = [], {}
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        plant_io = rig.plant_io

        converged = rig.converge(failures)
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )

        # The identical window — no scan reaches either peer, and no
        # field-side write lands.
        window = {
            "write": "none",
            "standby_role": pair.get(
                f"{standby_url}/role", "GET /role", failures
            ),
            "duty_role": pair.get(
                f"{duty_url}/role", "GET /role", failures
            ),
        }
        digest_entries.append({"phase": "window", **window})

        # The resumed pull's same-tick comparison matches the
        # untouched field: the standby stays tracking, and nothing
        # journals a divergence — the partition's staleness alone is
        # no conviction.
        pair.scan(standby_url, failures)
        report = pair.get(f"{standby_url}/role", "GET /role", failures)
        sync = report.get("sync")
        if report.get("role") != "standby" or not (
            isinstance(sync, dict) and "tracking" in sync
        ):
            failures.append(
                f"the write-free window left the standby reporting "
                f"{report} — the partition alone must not convict a "
                "tracking peer"
            )
            raise Abort
        journal = pair.get(
            f"{standby_url}/journal", "GET /journal", failures
        )
        if journal_events(journal, "divergence_detected"):
            failures.append(
                "the write-free window journaled a "
                "divergence_detected — a false conviction on "
                "partition staleness"
            )
            raise Abort
        digest_entries.append({"phase": "resumed", "report": report})

        # The documented switch: the tracking standby promotes
        # normally — the diverged half's refusal names the
        # divergence, not the window.
        switched = rig.switch(
            duty_url,
            standby_url,
            failures,
            promote_what="the tracking standby",
        )
        digest_entries.append(
            {
                "phase": "switch",
                "demote": switched["demote"],
                "promote": switched["promote"],
                "ticks": switched["ticks"],
                "demoted_role": switched["demoted_role"],
                "promoted_role": switched["promoted_role"],
            }
        )
        evidence["control_promoted"] = switched["promoted_role"]["tick"]

        # The launch roles restore, and the field stands the
        # restored owner's image.
        restored = rig.switch(
            standby_url,
            duty_url,
            failures,
            demote_what="the promoted peer",
            promote_what="the demoted peer",
        )
        field = refusal.field_out_samples(plant_io)
        for mismatch in refusal.field_mismatches(restored["owner"], field):
            failures.append(
                f"{mismatch} after the control's restore — the field "
                "does not hold the active's writes"
            )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "restored",
                "ticks": restored["ticks"],
                "demoted_role": restored["demoted_role"],
                "promoted_role": restored["promoted_role"],
            }
        )
    finally:
        if rig is not None:
            rig.close()
    return digest_entries, evidence


def divergence_pass(args, tamper):
    """The divergence run: the stale-standby refusal half, then the
    write-free control half. Returns `(digest_entries, evidence,
    failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the divergence "
            "leg has nothing to exercise"
        )
    with open(args.model) as handle:
        model = json.load(handle)
    point = carried_point(model)
    if point is None:
        raise Abort(
            f"the emitted model declares no carried boolean field "
            f"output bound to {CARRIED_SIGNAL} — the divergence leg "
            "has nothing to exercise"
        )
    digest_entries, evidence, failures = [], {"point": point}, []
    try:
        diverged, diverged_evidence = diverged_run(
            args, declared, point, tamper, failures
        )
        digest_entries.extend(diverged)
        evidence.update(diverged_evidence)
        control, control_evidence = control_run(args, declared, failures)
        digest_entries.extend(control)
        evidence.update(control_evidence)
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Exception as error:
        failures.append(f"the run raised {error!r}")
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
        choices=["skip-field-write"],
        help="doctor the run — the field-side write skipped while the "
        "diverged report and the refused promote are still asserted; "
        "the pass must fail naming the evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = divergence_pass(
            args, args.tamper
        )
    except Abort as abort:
        for line in abort.args:
            eprint(f"divergence: {line}")
        return 1
    for failure in failures:
        eprint(f"divergence: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"divergence: the {args.tamper} case passed silently — "
                "the leg never noticed the skipped field-side write"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"divergence-digest {digest} — diverged at tick "
        f"{evidence['diverged_at']} naming point {evidence['point']}, "
        f"promote refused not_converged with no field hand-off, "
        f"resolved at tick {evidence['resolved_at']}, run continued "
        f"to tick {evidence['final_tick']}; the control window "
        f"promoted at tick {evidence['control_promoted']} and "
        "restored the launch roles"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
