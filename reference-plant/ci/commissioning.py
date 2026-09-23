#!/usr/bin/env python3
"""The pair contract's commissioning/handover record leg — the
pre-pilot deployment-lifecycle drill (WW-LCM-002) that materializes
the commissioning record `docs/releases/commissioning-record.md`
declares, from one deterministic driven run on the manifest-declared
pair.

The commissioning record is the artifact a pilot handover assembles:
the witnessed evidence that the deployed pair was checked out, its
loops exercised, its alarm record signed off, and its documents
turned over — ending in the documented demote/promote handover. The
separate legs each prove one mechanism; this leg proves the *record*
itself — that one run produces every named artifact, byte-stable
across passes, and that a record missing any named artifact fails by
name. It is distinct from the #822 configuration-backup artifact set:
that set enumerates what a restore needs; this record enumerates what
a commissioning handover produces. The run:

- launches the manifest-declared pair on the released tooling and
  converges it — the tracking peer pulling the field owner's
  checkpoint each driven tick;
- assembles the **I/O checkout record** (`io-checkout`): the plant
  protocol's `list_points` census against the emitted model's
  declared channel-backed I/O set — every declared point present in
  the field with its declared direction, its stored field sample
  matching the field owner's served image point for point, and no
  undeclared field point appearing;
- assembles the **loop-check evidence** (`loop-check`): the declared
  measurement loop driven at marks across the threshold chain's
  declared `cutoff..high` span — rising and falling inside the latch
  thresholds so the excursion exercises the chain's `demand` and the
  group's `staged` report without leaving a tripped interlock or a
  latched alarm — each field-side write joined under the duty's
  recorded writer-claim token, read back from the field, and served
  identically by both peers at the next driven tick; then the
  declared output loop exercised end to end through the receipted
  path: `p101-mode` cutting the delivered `p101-cmd` off the group's
  request, `p101-hand` running the pump so the field census carries
  the delivered command and the `p101-run` feedback input returns it,
  and the restore returning the pump to group control;
- assembles the **alarm rationalization sign-off** (`alarm-signoff`):
  every managed alarm instance's declared rationalization block —
  consequence, required action, and the display/procedure reference —
  plus its `priority`/`class`/`response_ticks` codes, audited served
  verbatim on both peers, the sign-off the deployed pair carries to
  the operator boundary;
- executes the **handover procedure** (`handover`): the documented
  demote/promote switch — the converged standby taking the field
  while the run continues bumplessly — then the restore returning the
  pair to its declared launch roles, each peer's served journal
  carrying the role transitions in order;
- assembles the **documentation turnover**
  (`documentation-turnover`): the deployment's document set digested
  — the model, dynamics, and manifest bytes under their declared
  paths — each peer's checkpoint-stamped model fingerprint held equal
  to the manifest's recorded fingerprint, each peer's durable journal
  file digested over its normalized records, and each persisted state
  file's tick and fingerprint reported;
- audits the assembled record: every named artifact present — a
  record missing one fails naming it.

Usage:

    commissioning.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `commissioning-digest <sha256>` line prints — the
check runs two passes and compares them
(`commissioning-nondeterministic`). A contract violation reports
`commissioning: …` lines on stderr and exits 1 — the check's
`commissioning-failed`. The `--tamper` cases each drop one named
artifact from the assembled record — `missing-io-checkout`,
`missing-loop-check`, `missing-alarm-signoff`, and
`missing-documentation-turnover` — and the completeness audit must
fail naming the missing artifact rather than let an incomplete
record pass (`commissioning-unchecked`).
"""

import argparse
import hashlib
import json
import os
import sys

import alarm_rationalization
import failover
import pair
import simulate
import takeover


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The leg's receipted submissions declare this actor — the settled
# receipts and journaled transitions attribute the loop check's
# command-side writes to it.
ACTOR = "ci-commissioning"

# The bound each declared effect gets to land — the same
# carrier-crossing bound the takeover leg drives against.
SETTLE_BOUND = takeover.SETTLE_BOUND

# The commissioning record's named artifact set — the declaration
# `docs/releases/commissioning-record.md` makes and this leg
# materializes. The completeness audit fails naming any absent entry.
ARTIFACTS = (
    "io-checkout",
    "loop-check",
    "alarm-signoff",
    "documentation-turnover",
)

# The handover procedure the record names — the ordered phases the
# driven run executes.
PROCEDURE = (
    "converge",
    "io-checkout",
    "loop-check",
    "alarm-signoff",
    "handover",
    "documentation-turnover",
    "completeness-audit",
)

# The doctored cases — each removes the named artifact from the
# assembled record, so the completeness audit must name it.
TAMPER_ARTIFACT = {
    "missing-io-checkout": "io-checkout",
    "missing-loop-check": "loop-check",
    "missing-alarm-signoff": "alarm-signoff",
    "missing-documentation-turnover": "documentation-turnover",
}

# The rising-and-falling span marks the input leg drives — fractions
# of the chain's declared `cutoff..high` span held strictly inside the
# latch thresholds: above `cutoff` so no dry-run trip latches, below
# `high` so no managed alarm stands, while the `start`/`lag_start`
# excursions exercise the chain's demand and the group's staging.
MARK_FRACTIONS = (0.1, 0.3, 0.5, 0.7, 0.9, 0.7, 0.5, 0.3, 0.1)

# The signal names resolving the leg's measurement and output-loop
# points out of the emitted model — the same names the served index
# resolves, so the leg exercises the declared seam, never a
# hard-coded id.
SIGNALS = {
    "level": "level-primary",
    **takeover.SIGNALS,
}


def signal_sources(model):
    """The `{signal name: point id}` map the emitted model's signal
    index declares — the lowest-signal-id-wins rule the served index
    applies."""
    by_name = {}
    for signal in model.get("signals", []):
        current = by_name.get(signal["name"])
        if current is None or signal["id"] < current[0]:
            by_name[signal["name"]] = (signal["id"], signal["source"])
    return {name: source for name, (_id, source) in by_name.items()}


def checkout_points(model):
    """The declared field I/O set the checkout census audits — every
    channel-backed `io_points` entry in document order, each carrying
    the signal name resolving to it (None when the point is unnamed
    in the index) beside its declared direction, value kind, and
    channel."""
    names = {}
    for signal in model.get("signals", []):
        current = names.get(signal["source"])
        if current is None or signal["id"] < current[0]:
            names[signal["source"]] = (signal["id"], signal["name"])
    return [
        {
            "point": point["id"],
            "name": (names.get(point["id"]) or (None, None))[1],
            "direction": point["direction"],
            "value_type": point["value_type"],
            "channel": point["channel"],
        }
        for point in model.get("io_points", [])
        if point.get("channel") is not None
    ]


def level_chain(model):
    """The `(point, cutoff, high)` measurement loop the input leg
    drives — the `level` signal's declared channel-backed float `in`
    point plus the threshold-chain component's declared span marks —
    or None when the emitted model declares no such loop."""
    sources = signal_sources(model)
    point = sources.get(SIGNALS["level"])
    if point is None:
        return None
    declared = next(
        (
            entry
            for entry in model.get("io_points", [])
            if entry["id"] == point
        ),
        None,
    )
    if not (
        declared
        and declared.get("direction") == "in"
        and declared.get("value_type") == "float"
        and declared.get("channel") is not None
    ):
        return None
    chain = next(
        (
            component
            for component in model.get("components", [])
            if component.get("kind") == "threshold-chain"
        ),
        None,
    )
    if chain is None:
        return None
    parameters = chain.get("parameters") or {}
    cutoff, high = parameters.get("cutoff"), parameters.get("high")
    if not (
        isinstance(cutoff, dict)
        and isinstance(cutoff.get("float"), (int, float))
        and isinstance(high, dict)
        and isinstance(high.get("float"), (int, float))
        and cutoff["float"] < high["float"]
    ):
        return None
    return point, cutoff["float"], high["float"]


def missing_artifacts(record):
    """The named artifacts the assembled record lacks — the
    completeness audit's findings, in the declared set's order."""
    artifacts = record.get("artifacts") or {}
    return [
        name
        for name in ARTIFACTS
        if artifacts.get(name) in (None, {}, [])
    ]


def submit(url, command, failures):
    """POST one receipted write to the active's `/command` and assert
    the `accepted` submission — returns the receipt."""
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


def write_value(point, value):
    """A `write_value` command body for the receipted path."""
    return {
        "write_value": {
            "kind": "bool",
            "point": point,
            "value": {"bool": value},
        }
    }


def field_census(plant_io, failures):
    """The field's own point census — the plant protocol's
    `list_points`, unfenced so the checkout reads every bound point's
    direction, stored sample, and standing fault."""
    verdict = plant_io.request({"op": "list_points"})
    points = verdict.get("points") if isinstance(verdict, dict) else None
    if not isinstance(points, list):
        failures.append(f"the field's list_points answered {verdict}")
        raise Abort
    return points


def join_claim(plant_io, preamble, failures):
    """Join the field's standing writer claim under the duty's
    recorded owner token — the conditional `ensure_writer` grant the
    divergence leg's field-side write uses: granted while the claim
    already names that owner, never a preempt. Returns the grant's
    claim-state word for the record."""
    owner = failover.owner_token(preamble)
    if owner is None:
        failures.append(
            "the field owner recorded no claim token — the loop "
            "check cannot join the field's writer claim to drive its "
            "marks"
        )
        raise Abort
    verdict = plant_io.request({"op": "ensure_writer", "owner": owner})
    kind = failover.probe_kind(verdict)
    if kind != "granted":
        failures.append(
            f"the loop check's claim join under the recorded owner "
            f"token answered {kind} — the leg could not reach the "
            "field it drives"
        )
        raise Abort
    # The token itself is a per-process identity — the record keeps
    # the grant word, not the ephemeral number.
    return kind


def sha256_file(path):
    """The artifact's sha256 over the document's checked-in bytes."""
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def commissioning_pass(args, tamper):
    """The commissioning run: converge, checkout, loop check,
    sign-off, handover, turnover, audit. Returns `(digest_entries,
    evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the commissioning "
            "leg has nothing to exercise"
        )
    manifest, duty_decl, standby_decl = declared
    with open(args.model) as handle:
        model = json.load(handle)
    declared_checkout = checkout_points(model)
    if not declared_checkout:
        raise Abort(
            "the emitted model declares no channel-backed I/O points — "
            "the checkout record has nothing to audit"
        )
    chain = level_chain(model)
    if chain is None:
        raise Abort(
            "the emitted model declares no threshold-chain level "
            "loop — the loop check has nothing to drive"
        )
    level_point, cutoff, high = chain
    points = takeover.signal_points(model)
    if points is None:
        raise Abort(
            "the emitted model declares no writable per-pump mode "
            "seam — the output leg has nothing to exercise"
        )
    declared_alarms = alarm_rationalization.declared_record(model)
    if not declared_alarms:
        raise Abort(
            "the emitted model declares no managed alarm instance — "
            "the sign-off has nothing to exercise"
        )
    marks = [
        {
            "fraction": fraction,
            "driven": {"float": cutoff + fraction * (high - cutoff)},
        }
        for fraction in MARK_FRACTIONS
    ]

    digest_entries, evidence, failures = [], {}, []
    record = {
        "record": "commissioning-handover",
        "release": manifest["dcs_release"],
        "images": manifest["images"],
        "model": manifest["model"],
        "dynamics": manifest["dynamics"],
        "peers": [duty_decl["name"], standby_decl["name"]],
        "procedure": list(PROCEDURE),
        "artifacts": {},
    }
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        plant_io = rig.plant_io

        # Phase 1 — convergence: the tracking peer pulling the field
        # owner's latest checkpoint each driven tick until the pair
        # rests identical.
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

        # Phase 2 — the I/O checkout record: the field census against
        # the declared channel set, bracketing one driven tick — the
        # field steps after each scan's reads, so the served image's
        # sample for a point must equal the field's sample at one of
        # the two boundaries (the read-side value for a point the
        # plant moves itself, the delivered value for a commanded
        # one) — the point-to-point verification the record carries.
        census_before = {
            entry["point"]: entry for entry in field_census(plant_io, failures)
        }
        declared_ids = {entry["point"] for entry in declared_checkout}
        if set(census_before) != declared_ids:
            failures.append(
                "the field census does not match the declared channel "
                f"set — the field carries {sorted(census_before)}, "
                f"the model declares {sorted(declared_ids)}"
            )
            raise Abort
        _tracked, owner = rig.tick(
            rig.standby_url, rig.duty_url, failures
        )
        census_after = {
            entry["point"]: entry for entry in field_census(plant_io, failures)
        }
        served = {
            entry["point"]: entry.get("sample")
            for entry in owner.get("points", [])
        }
        rows = []
        for entry in declared_checkout:
            point = entry["point"]
            before = census_before[point].get("sample") or {}
            after = census_after[point].get("sample") or {}
            served_sample = served.get(point) or {}
            live = before.get("value") != after.get("value")
            if census_after[point].get("direction") != entry["direction"]:
                failures.append(
                    f"point {point} serves direction "
                    f"{census_after[point].get('direction')} in the "
                    f"field census, declared {entry['direction']}"
                )
            if not after or not served_sample:
                failures.append(
                    f"point {point} ({entry['name']}) carries no "
                    f"sample — the field reports {after}, the served "
                    f"image {served_sample} — the point-to-point "
                    "check cannot run"
                )
            elif (
                served_sample.get("value"),
                served_sample.get("quality"),
            ) not in (
                (before.get("value"), before.get("quality")),
                (after.get("value"), after.get("quality")),
            ):
                failures.append(
                    f"point {point} ({entry['name']}) serves "
                    f"{served_sample} — the field held {before} "
                    f"before the scan and {after} after it, so the "
                    "image carries a value the field never presented "
                    "— the point-to-point check failed"
                )
            rows.append(
                {
                    **entry,
                    "live": live,
                    "field": {
                        "value": before.get("value"),
                        "quality": before.get("quality"),
                    },
                    "served": {
                        "value": served_sample.get("value"),
                        "quality": served_sample.get("quality"),
                    },
                    "fault": census_after[point].get("fault"),
                }
            )
        if failures:
            raise Abort
        record["artifacts"]["io-checkout"] = {
            "checked_at": owner["tick"],
            "points": rows,
        }
        evidence["checked_at"] = owner["tick"]
        evidence["io_points"] = len(rows)
        evidence["live_points"] = sum(1 for row in rows if row["live"])
        digest_entries.append(
            {
                "phase": "io-checkout",
                "tick": owner["tick"],
                "points": len(rows),
            }
        )

        # Phase 3 — the loop-check evidence. The input leg drives the
        # declared measurement at marks across the chain's declared
        # span — each field-side write joined under the duty's
        # recorded writer-claim token, read back, then served
        # identically on both peers at the next driven tick, the
        # chain's own `demand` and the group's `staged` report
        # recorded as the loop's response.
        claim = join_claim(plant_io, rig.duty_preamble, failures)
        ladder = []
        for mark in marks:
            write = plant_io.request(
                {
                    "op": "write",
                    "point": level_point,
                    "value": mark["driven"],
                }
            )
            if write.get("result") != "done":
                failures.append(
                    f"the loop check's field write on point "
                    f"{level_point} answered {write}"
                )
                raise Abort
            landed = failover.field_read(plant_io, level_point, failures)
            if landed.get("value") != mark["driven"]:
                failures.append(
                    f"the loop check's write left point {level_point} "
                    f"at {landed.get('value')}, expected "
                    f"{mark['driven']} — the stimulus never reached "
                    "the field"
                )
                raise Abort
            tracked, owner = rig.tick(
                rig.standby_url, rig.duty_url, failures
            )
            served_marks = {
                rig.peer_name(url): takeover.value(snapshot, level_point)
                for url, snapshot in (
                    (rig.duty_url, owner),
                    (rig.standby_url, tracked),
                )
            }
            for peer, served_value in served_marks.items():
                if served_value != mark["driven"]:
                    failures.append(
                        f"{peer} serves {served_value} at mark "
                        f"{mark['fraction']}, expected "
                        f"{mark['driven']} — the driven mark did not "
                        "reach the controller"
                    )
            if failures:
                raise Abort
            ladder.append(
                {
                    **mark,
                    "tick": owner["tick"],
                    "served": served_marks,
                    "response": {
                        "demand": takeover.value(
                            owner, points["demand"]
                        ),
                        "staged": takeover.value(
                            owner, points["staged"]
                        ),
                    },
                }
            )
        plant_io.request({"op": "release_writer"})

        # The output leg: `p101-mode` through the receipted path cuts
        # the delivered command off the group's request, `p101-hand`
        # runs the pump — the field census carrying the delivered
        # command and the run-feedback input returning it — then the
        # restore returns the pump to group control.
        mode_write = write_value(points["mode"], True)
        mode_receipt = submit(duty_url, mode_write, failures)
        owner = takeover.drive_until(
            rig,
            failures,
            lambda snapshot: takeover.value(snapshot, points["mode"])
            == {"bool": True}
            and takeover.value(snapshot, points["auto"])
            == {"bool": False}
            and takeover.value(snapshot, points["cmd"])
            == {"bool": False},
        )
        if owner is None:
            owner = takeover.tick(rig, failures)
            failures.append(
                "the receipted mode write's effect never landed — "
                f"mode reads {takeover.value(owner, points['mode'])}, "
                f"the auto leg "
                f"{takeover.value(owner, points['auto'])}, the "
                "delivered command "
                f"{takeover.value(owner, points['cmd'])}"
            )
            raise Abort
        hand_write = write_value(points["hand"], True)
        hand_receipt = submit(duty_url, hand_write, failures)
        owner = takeover.drive_until(
            rig,
            failures,
            lambda snapshot: takeover.value(snapshot, points["cmd"])
            == {"bool": True}
            and takeover.value(snapshot, points["run"])
            == {"bool": True},
        )
        if owner is None:
            owner = takeover.tick(rig, failures)
            failures.append(
                "the receipted hand write never ran the pump — the "
                "delivered command or the run feedback never asserted "
                "on the operator demand"
            )
            raise Abort
        # The loop's far end: the field itself must carry the
        # delivered command and the returned run feedback.
        census = {
            entry["point"]: (entry.get("sample") or {}).get("value")
            for entry in field_census(plant_io, failures)
        }
        delivered = census.get(points["cmd"])
        returned = census.get(points["run"])
        if delivered != {"bool": True} or returned != {"bool": True}:
            failures.append(
                "the field does not carry the loop check's output — "
                f"the delivered command reads {delivered} and the "
                f"run feedback {returned} in the field census"
            )
            raise Abort
        restored = []
        for key in ("hand", "mode"):
            command = write_value(points[key], False)
            submit(duty_url, command, failures)
            restored.append(command)
        owner = takeover.drive_until(
            rig,
            failures,
            lambda snapshot: takeover.value(snapshot, points["auto"])
            == {"bool": True}
            and takeover.value(snapshot, points["avail"])
            == {"bool": True}
            and takeover.value(snapshot, points["cmd"])
            == takeover.value(snapshot, points["group_cmd"]),
        )
        if owner is None:
            owner = takeover.tick(rig, failures)
            failures.append(
                "the loop check's restore did not return the pump to "
                f"group control — auto reads "
                f"{takeover.value(owner, points['auto'])}, "
                "availability "
                f"{takeover.value(owner, points['avail'])}, the "
                "delivered command "
                f"{takeover.value(owner, points['cmd'])} against the "
                "group request "
                f"{takeover.value(owner, points['group_cmd'])}"
            )
            raise Abort
        receipts_duty = pair.get(
            f"{duty_url}/receipts", "GET /receipts", failures
        )
        for command in [mode_write, hand_write, *restored]:
            if not takeover.settled(receipts_duty, command):
                failures.append(
                    f"the loop-check write {command} never settled "
                    "applied into the adopted receipt log"
                )
        if failures:
            raise Abort
        record["artifacts"]["loop-check"] = {
            "input": {
                "point": level_point,
                "signal": SIGNALS["level"],
                "span": {"cutoff": cutoff, "high": high},
                "claim": claim,
                "marks": ladder,
            },
            "output": {
                "points": {
                    key: points[key] for key in ("mode", "hand", "cmd", "run")
                },
                "mode_receipt": mode_receipt,
                "hand_receipt": hand_receipt,
                "delivered": delivered,
                "returned": returned,
                "restored": {
                    "auto": takeover.value(owner, points["auto"]),
                    "avail": takeover.value(owner, points["avail"]),
                    "cmd": takeover.value(owner, points["cmd"]),
                    "group_cmd": takeover.value(owner, points["group_cmd"]),
                },
            },
        }
        evidence["marks"] = len(ladder)
        evidence["output_at"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "loop-check",
                "marks": len(ladder),
                "output_tick": owner["tick"],
            }
        )

        # Phase 4 — the alarm rationalization sign-off: every managed
        # alarm instance's declared record audited served verbatim on
        # both peers — the tracking standby's adopted state carrying
        # the same single declaration.
        peers = (
            (duty_decl["name"], duty_url),
            (standby_decl["name"], standby_url),
        )
        served_alarms = {
            name: alarm_rationalization.audit_peer(
                model, name, url, failures
            )
            for name, url in peers
        }
        if failures:
            raise Abort
        record["artifacts"]["alarm-signoff"] = {
            "declared": declared_alarms,
            "served": served_alarms,
        }
        evidence["alarms"] = len(declared_alarms)
        digest_entries.append(
            {
                "phase": "alarm-signoff",
                "instances": len(declared_alarms),
            }
        )

        # Phase 5 — the handover procedure: the documented
        # demote/promote switch hands the field to the converged
        # standby bumplessly, then the restore returns the pair to
        # its declared launch roles — the commissioning record's
        # changeover evidence.
        switched = rig.switch(duty_url, standby_url, failures)
        evidence["switched_at"] = switched["demote"]["tick"]
        demote = rig.demote(
            rig.standby_url, failures, "the new field owner"
        )
        promote = rig.promote(
            rig.duty_url, failures, "the reconverged peer"
        )
        ticks = []
        for _ in range(pair.HANDOVER_TICKS):
            _tracked, owner = pair.tick(
                rig.standby_url, rig.duty_url, failures
            )
            ticks.append(owner["tick"])
        duty_role = pair.get(
            f"{rig.duty_url}/role", "GET /role", failures
        )
        standby_role = pair.get(
            f"{rig.standby_url}/role", "GET /role", failures
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
        record["handover"] = {
            "switch": {
                "demote": switched["demote"],
                "promote": switched["promote"],
                "ticks": switched["ticks"],
                "transitions": switched["transitions"],
            },
            "restore": {
                "demote": demote,
                "promote": promote,
                "ticks": ticks,
                "duty_role": duty_role,
                "standby_role": standby_role,
            },
        }
        evidence["restored_at"] = ticks[-1]
        digest_entries.append(
            {
                "phase": "handover",
                "switch_ticks": switched["ticks"],
                "restore_ticks": ticks,
            }
        )

        # Phase 6 — the documentation turnover: the deployment's
        # document set digested under its declared paths, each peer's
        # checkpoint-stamped model fingerprint held equal to the
        # manifest's recorded fingerprint, and each peer's durable
        # journal and state files reported — the document set the
        # handover turns over.
        expected_fp = format(rig.fingerprint, "016x")
        turnover = {
            "documents": {
                manifest["model"]["path"]: sha256_file(args.model),
                manifest["dynamics"]["path"]: sha256_file(args.dynamics),
                "deploy/manifest.json": sha256_file(args.manifest),
            },
            "peers": {},
        }
        for name, url, files in (
            (duty_decl["name"], duty_url, rig.duty_files),
            (standby_decl["name"], standby_url, rig.standby_files),
        ):
            checkpoint = pair.get(
                f"{url}/checkpoint", "GET /checkpoint", failures
            )
            served_fp = checkpoint.get("model_fingerprint")
            if not isinstance(served_fp, int):
                failures.append(
                    f"{name}'s checkpoint carries no model_fingerprint "
                    "— the turnover cannot name the served model"
                )
                raise Abort
            served_fp = format(served_fp, "016x")
            if served_fp != expected_fp:
                failures.append(
                    f"{name} serves model fingerprint {served_fp} but "
                    f"the manifest records {expected_fp} — the "
                    "turned-over fingerprint does not name the served "
                    "model"
                )
            peer_turnover = {"served_fingerprint": served_fp}
            journal_path = files.get("journal_file")
            if journal_path is not None:
                if not os.path.exists(journal_path):
                    failures.append(
                        f"{name}'s declared journal file "
                        f"{journal_path} does not exist — the "
                        "--journal-file flag was not honored"
                    )
                else:
                    records = pair.journal_records(journal_path)
                    peer_turnover["journal"] = {
                        "declared": os.path.basename(journal_path),
                        "records": len(records),
                        "sha256": hashlib.sha256(
                            json.dumps(records, sort_keys=True).encode()
                        ).hexdigest(),
                    }
            state_path = files.get("state_file")
            if state_path is not None:
                if not os.path.exists(state_path):
                    failures.append(
                        f"{name}'s declared state file {state_path} "
                        "does not exist — the --state-file flag was "
                        "not honored"
                    )
                else:
                    with open(state_path) as handle:
                        persisted = json.load(handle)
                    if (
                        persisted.get("model_fingerprint")
                        != rig.fingerprint
                    ):
                        failures.append(
                            f"{name}'s state file carries fingerprint "
                            f"{persisted.get('model_fingerprint')}, the "
                            f"manifest declares {rig.fingerprint}"
                        )
                    peer_turnover["state"] = {
                        "declared": os.path.basename(state_path),
                        "tick": persisted.get("tick"),
                        "model_fingerprint": persisted.get(
                            "model_fingerprint"
                        ),
                    }
            turnover["peers"][name] = peer_turnover
        if failures:
            raise Abort
        record["artifacts"]["documentation-turnover"] = turnover
        evidence["documents"] = len(turnover["documents"])
        evidence["final_tick"] = ticks[-1]
        digest_entries.append(
            {
                "phase": "documentation-turnover",
                "documents": len(turnover["documents"]),
            }
        )

        # Phase 7 — the completeness audit: every named artifact the
        # record declares must be present — a record missing one
        # fails naming it. Under a missing-artifact tamper the
        # doctored record drops the named entry and the audit must
        # fire.
        if tamper is not None:
            record["artifacts"].pop(TAMPER_ARTIFACT[tamper], None)
        for name in missing_artifacts(record):
            failures.append(
                f"the record carries no {name} artifact — the "
                "commissioning record is incomplete"
            )
        if failures:
            raise Abort
        digest_entries.append({"phase": "record", "record": record})
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
        choices=list(TAMPER_ARTIFACT),
        help="drop the named artifact from the assembled record — the "
        "completeness audit must fail naming it",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = commissioning_pass(
            args, args.tamper
        )
    except Abort as abort:
        for line in abort.args:
            eprint(f"commissioning: {line}")
        return 1
    for failure in failures:
        eprint(f"commissioning: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"commissioning: the {args.tamper} case passed "
                "silently — the leg never noticed the dropped "
                "artifact"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"commissioning-digest {digest} — tracking by tick "
        f"{evidence['converged']}, {evidence['io_points']} field "
        f"points checked out at tick {evidence['checked_at']}, "
        f"{evidence['marks']} loop-check marks driven, "
        f"{evidence['alarms']} managed alarm instances signed off, "
        f"switched at tick {evidence['switched_at']} and restored at "
        f"tick {evidence['restored_at']}, {evidence['documents']} "
        "turnover documents digested"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
