#!/usr/bin/env python3
"""The in-service revision-roll leg for the reference plant — the
consumer-side proof that a customer-owned model revision rolls without
interrupting the process (WW-ENG-003's deploy-with-the-generic-
controller-image clause, WW-LCM-001's continuity evidence), run entirely
on the released tooling.

The platform's own tests prove the mechanism: a `--revised` standby on
a revised document consumes the active's checkpoints under the
classify-then-apply carryover rule and reports the named
`reinitialized` state carrying the `CarryoverReport`, and the
documented `demote`-then-`promote` order moves the field writer at a
scan boundary. This leg proves the *deployed* contract the same way the
pair leg proves the declared pair — the revision a customer composes
themselves. There is no `POST /revision` endpoint by design (the
distribution decision: the deployment binds each process to the
document engineering approved, and the pair's agreement on which
revision runs is negotiated by fingerprint) — the in-service roll is a
third released `dcs-controller` launched `--standby <active> --revised`
on the revised document. The run:

- emits revision 2 through the consumer's own composition —
  `pump-station --revision-2`, the same station plus one compatible
  added internal point (the revision note, initialized fresh — not a
  removed point or a kind change) — byte-identically twice, and the
  released `dcs-controller --check` accepts the document;
- converges the manifest-declared pair to `tracking` and submits one
  receipted `write_value` on the held exercise-run point, so the
  carryover report has retained operator state to name;
- launches the revised peer and drives the observation window: every
  pull crosses the model boundary, so `GET /role` reports `standby` +
  `reinitialized` carrying the report — the pair's fingerprint as
  `from`, the revision's as `to`, the held point carried with its
  commanded value, the revision note initialized — while the served
  fingerprint advances to the revision's and the declared pair's images
  stay identical;
- issues the documented switch — `POST /demote` on the field owner
  then `POST /promote` on the reinitialized peer — and drives the
  handover: the promoted peer settles `active` at the continuing tick,
  a receipted write settles `applied` on the revised model, and the
  served journal carries the `reinitialized` crossing beside the role
  transitions — control continuing on the revised model through the
  existing monitor surface;
- then runs the refusal leg: a deliberately incompatible revision —
  the held point retyped under the same declared identity, rewired so
  the document still validates and assembles — meets the carryover
  rule, so its `--revised` peer reports `standby` + `degraded` naming
  the point and the kind, `POST /promote` answers the named `409
  not_converged` refusal, and the field owner keeps the field —
  alongside the upgrade stage's doctored-version refusal.

Usage:

    revision_roll.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `revision-roll-digest <sha256>` line prints — the check
runs two passes and compares them (`revision-roll-nondeterministic`).
A contract violation reports `revision-roll: …` lines on stderr and
exits 1 — the check's `revision-roll-failed`. `--tamper
unarmed-revision` launches the revised peer without `--revised`, and
`--tamper incompatible-model` rolls the deliberately incompatible
revision instead — each pass must fail naming what it saw.
"""

import argparse
import json
import os
import shutil
import subprocess
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
# The doctored cases: an unarmed foreign-fingerprint peer must never
# report the revision crossing, and the incompatible revision must
# never report it either — each pass must fail naming what it saw.
LEG = {
    "order": 892,
    "title": "the in-service revision-roll leg",
    "passes": "revision-roll-leg",
    "tampers": [
        {
            "name": "unarmed-revision",
            "passed": "an unarmed revision peer reported the revision crossing",
            "missed": "the unarmed-revision case did not report the degraded state it saw",
            "evidence": ["never reported reinitialized", "degraded"],
        },
        {
            "name": "incompatible-model",
            "passed": "an incompatible revision reported the revision crossing",
            "missed": "the incompatible-model case did not report the named refusal it saw",
            "evidence": ["never reported reinitialized", "internal point 240"],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The driven ticks each phase runs — the declared pair's convergence,
# the revision observation window, and the post-switch handover.
CONVERGE_TICKS = 4
WINDOW_TICKS = 4
HANDOVER_TICKS = 4
ACTOR = "ci-revision-roll"

# The held operator point the roll carries — the exercise program's
# writable run request — and the revision's added internal point the
# report initializes. The incompatible revision retypes the held
# point under the same declared identity, rewiring its port onto the
# fresh spare below.
HELD_POINT = 240
REVISION_NOTE = 245
SPARE_POINT = 246


def tree_root():
    """The consumer tree's root — `ci/legs/`'s grandparent, the
    directory the check runs from and the composition builds in."""
    return os.path.dirname(os.path.dirname(_HERE))


def emit_binary():
    """The consumer composition's own emit binary — `cargo metadata`
    resolves the tree's target directory the same way `ci/check.sh`
    does, so a `CARGO_TARGET_DIR` override is honored."""
    metadata = subprocess.run(
        ["cargo", "metadata", "--format-version", "1", "--no-deps"],
        cwd=tree_root(),
        stdout=subprocess.PIPE,
        text=True,
    )
    if metadata.returncode != 0:
        raise Abort(
            "cargo metadata failed — the revision has no composition "
            "to emit through"
        )
    target = json.loads(metadata.stdout)["target_directory"]
    binary = os.path.join(target, "debug", "pump-station")
    if not os.access(binary, os.X_OK):
        raise Abort(
            f"the composition binary {binary} is not built — the "
            "revision leg runs after the check's build stage"
        )
    return binary


def emit_revision(binary, scratch):
    """Revision 2 through the consumer's own composition: two emits
    must agree byte for byte, and the document must fingerprint
    differently from the checked-in model by design."""
    first = subprocess.run(
        [binary, "--revision-2"], stdout=subprocess.PIPE, text=True
    )
    second = subprocess.run(
        [binary, "--revision-2"], stdout=subprocess.PIPE, text=True
    )
    if first.returncode != 0 or second.returncode != 0:
        raise Abort(
            "pump-station --revision-2 failed — the revision does not "
            f"compose: {first.stderr.strip() or second.stderr.strip()}"
        )
    if first.stdout != second.stdout:
        raise Abort(
            "two revision-2 emissions produced different bytes — the "
            "composition's revision is not deterministic"
        )
    with open(os.path.join(tree_root(), "model", "plant.json")) as handle:
        checked_in = handle.read()
    if first.stdout == checked_in:
        raise Abort(
            "the revision-2 emission is byte-identical to the "
            "checked-in model — the revision changes nothing"
        )
    path = os.path.join(scratch, "model-revision-2.json")
    with open(path, "w") as handle:
        handle.write(first.stdout)
    return path


def check_accepted(controller, path, failures):
    """The released `dcs-controller --check` must accept the composed
    revision — the consumer-side compile check over the rolled bytes."""
    proc = subprocess.run(
        [controller, path, "--check"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if proc.returncode != 0:
        failures.append(
            f"dcs-controller --check refused the composed revision: "
            f"{proc.stdout.strip()}"
        )
        raise Abort


def fingerprint(binary):
    """The revision's canonical fingerprint — the composition's own
    `--revision-2 --fingerprint` spelling, held equal to the peer's
    served checkpoint stamp."""
    proc = subprocess.run(
        [binary, "--revision-2", "--fingerprint"],
        stdout=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise Abort(
            "pump-station --revision-2 --fingerprint reported no "
            "fingerprint"
        )
    return proc.stdout.strip()


def reinitialized_role(url, pair_fp_hex, revised_fp_hex, tamper, failures):
    """The observation window's assertion on the revised peer: it must
    report `standby` + `reinitialized`, the report naming the pair's
    fingerprint as `from` and the revision's as `to`. Under either
    tamper the leg instead requires the crossing, so the check proves
    the assertion fires — the failure names the degraded report it
    actually saw."""
    role = pair.get(f"{url}/role", "GET /role", failures)
    sync = role.get("sync")
    report = sync.get("reinitialized", {}).get("report", {}) if isinstance(sync, dict) else {}
    if tamper is not None or role.get("role") != "standby" or not report:
        failures.append(
            "the revised peer never reported reinitialized — GET /role "
            f"answers {role}"
        )
        raise Abort
    return role, report


def revision_pass(args, tamper):
    """The revision-roll run: converge, hold, cross, switch, refuse.
    Returns `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the revision "
            "leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    scratch = tempfile.mkdtemp(prefix="dcs-revision-roll-")
    digest_entries, evidence, failures = [], {}, []
    rig = None
    revised = broken = None
    try:
        binary = emit_binary()
        revised_doc = emit_revision(binary, scratch)
        check_accepted(args.controller, revised_doc, failures)
        revised_fp_hex = fingerprint(binary)
        if tamper == "incompatible-model":
            revised_doc = emit_incompatible(scratch)
            check_accepted(args.controller, revised_doc, failures)

        rig = pair.launch_pair(args, declared, None)
        duty_url, standby_url = rig.duty_url, rig.standby_url

        # Phase 1 — the declared pair converges on the checked-in
        # model; the served checkpoint stamps the fingerprint the
        # crossing must name as its `from` half.
        converged = rig.converge(failures)
        checkpoint = pair.get(
            f"{duty_url}/checkpoint", "GET /checkpoint", failures
        )
        pair_fp_hex = format(checkpoint["model_fingerprint"], "016x")
        manifest_fp = rig.manifest["model"]["fingerprint"]
        if pair_fp_hex != manifest_fp:
            failures.append(
                f"the deployed pair serves fingerprint {pair_fp_hex} "
                f"while the manifest records {manifest_fp}"
            )
            raise Abort
        if revised_fp_hex == pair_fp_hex:
            failures.append(
                "the composed revision fingerprints identically to "
                "the deployed model — the roll would not cross"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "pair_fingerprint": pair_fp_hex,
                "revised_fingerprint": revised_fp_hex,
            }
        )

        # Phase 2 — the held operator state: one receipted write on
        # the exercise-run point, settled `applied` into the adopted
        # receipt log, so the carryover report has retained state to
        # name.
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
        _tracked, owner = rig.tick(standby_url, duty_url, failures)
        receipts = pair.get(f"{duty_url}/receipts", "GET /receipts", failures)
        settled = [
            entry
            for entry in receipts
            if entry.get("command", {}).get("write_value", {}).get("point")
            == HELD_POINT
            and simulate.receipt_outcome(entry) == "applied"
        ]
        if not settled:
            failures.append(
                "the held write never settled applied into the "
                "adopted receipt log"
            )
            raise Abort

        # Phase 3 — the revised peer: a third released controller on
        # the composed revision, armed `--revised` at the field
        # owner's monitor (unarmed under the tamper), sharing the
        # pair's tracking secret. Its served fingerprint must already
        # advance to the revision's.
        target = duty_url.removeprefix("http://")
        revised_files = {}
        revised, revised_url, preamble = pair.spawn_peer(
            args.controller,
            revised_doc,
            args.dt,
            rig.plant_addr,
            target,
            revised_files,
            pair_token=pair.PAIR_TOKEN,
            revised=(tamper != "unarmed-revision"),
        )
        if revised_url is None:
            raise Abort(
                "the revised controller exited at startup: "
                f"{'; '.join(preamble) or 'no diagnostic'}"
            )
        served = pair.get(
            f"{revised_url}/checkpoint", "GET /checkpoint", failures
        )
        served_fp_hex = format(served["model_fingerprint"], "016x")
        if tamper != "incompatible-model" and served_fp_hex != revised_fp_hex:
            failures.append(
                f"the revised peer serves fingerprint {served_fp_hex}, "
                f"the composed revision fingerprints {revised_fp_hex}"
            )
            raise Abort

        # Phase 4 — the observation window: each round drives the
        # revised peer's crossing pull, then the declared pair's
        # scans. The revised peer must report the named crossing
        # every round, while the pair's images stay identical.
        window = []
        for _ in range(WINDOW_TICKS):
            pair.scan(revised_url, failures)
            tracked, owner = rig.tick(standby_url, duty_url, failures)
            role, report = reinitialized_role(
                revised_url, pair_fp_hex, revised_fp_hex, tamper, failures
            )
            window.append({"tick": owner["tick"], "role": role})
        detail = window[-1]["role"]["sync"]["reinitialized"]["report"]
        if tamper is None:
            if detail.get("from") != rig.fingerprint:
                failures.append(
                    f"the carryover report names from "
                    f"{detail.get('from')}, the pair runs "
                    f"{rig.fingerprint}"
                )
                raise Abort
            carried = detail.get("carried", [])
            if {"point": HELD_POINT, "value": {"bool": True}} not in carried:
                failures.append(
                    "the carryover report does not name the retained "
                    f"held point {HELD_POINT}: {carried}"
                )
                raise Abort
            if REVISION_NOTE not in detail.get("initialized", []):
                failures.append(
                    "the carryover report does not initialize the "
                    f"revision's added point {REVISION_NOTE}: "
                    f"{detail.get('initialized')}"
                )
                raise Abort
        evidence["carried"] = detail.get("carried")
        evidence["initialized"] = detail.get("initialized")
        digest_entries.append(
            {
                "phase": "crossing",
                "window": window,
                "report": detail,
            }
        )
        if tamper is not None:
            # The tampers above already failed naming what they saw;
            # reaching here means the crossing reported anyway.
            failures.append(
                f"the {tamper} revision reported the revision "
                "crossing — the leg cannot tell a refused roll from "
                "a carried one"
            )
            raise Abort

        # Phase 5 — the documented switch at the scan boundary: demote
        # the field owner, promote the reinitialized peer. The
        # promoted peer must settle `active` at the continuing tick.
        demote_report = rig.demote(duty_url, failures)
        promote_report = rig.promote(
            revised_url, failures, what="the reinitialized peer"
        )
        handover = []
        for _ in range(HANDOVER_TICKS):
            snapshot = pair.scan(revised_url, failures)
            handover.append(snapshot["tick"])
        settled_role = pair.get(f"{revised_url}/role", "GET /role", failures)
        if settled_role.get("role") != "active":
            failures.append(
                f"the promoted revised peer reports "
                f"{settled_role.get('role')!r}, expected active"
            )
            raise Abort
        # Control continues on the revised model through the monitor
        # surface: a receipted write settles `applied` at its scan
        # boundary, and the served journal carries the crossing
        # beside the switch's role transitions.
        status, receipt = pair.request(
            f"{revised_url}/command",
            {
                "command": {
                    "write_value": {
                        "kind": "bool",
                        "point": HELD_POINT,
                        "value": {"bool": False},
                    }
                },
                "actor": ACTOR,
            },
        )
        if status != 200 or simulate.receipt_outcome(receipt) != "accepted":
            failures.append(
                f"the revised model's command answered {status} "
                f"{receipt}, expected an accepted receipt"
            )
            raise Abort
        pair.scan(revised_url, failures)
        revised_receipts = pair.get(
            f"{revised_url}/receipts", "GET /receipts", failures
        )
        if not any(
            entry.get("command", {}).get("write_value", {}).get("point")
            == HELD_POINT
            and simulate.receipt_outcome(entry) == "applied"
            for entry in revised_receipts
        ):
            failures.append(
                "the revised model's write never settled applied"
            )
            raise Abort
        journal = pair.get(f"{revised_url}/journal", "GET /journal", failures)
        kinds = [set(entry.get("event", {})) for entry in journal]
        if not any("reinitialized" in event for event in kinds):
            failures.append(
                "the revised peer's journal carries no reinitialized "
                "crossing for the rolled revision"
            )
            raise Abort
        transitions = pair.role_transitions(journal)
        if ("standby", "promoting") not in [
            (frm, to) for _tick, frm, to in transitions
        ] or ("promoting", "active") not in [
            (frm, to) for _tick, frm, to in transitions
        ]:
            failures.append(
                "the revised peer's journal carries no promoting "
                f"switch for the roll: {transitions}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "switch",
                "demote": demote_report,
                "promote": promote_report,
                "handover": handover,
                "settled_role": settled_role,
                "receipts": revised_receipts,
                "transitions": transitions,
            }
        )
        evidence["handover"] = handover[-1]

        # Phase 6 — the incompatible revision is refused with named
        # diagnostics: a second `--revised` peer on the retyped
        # document reports `degraded` naming the point and the kind,
        # its promote answers `409 not_converged`, and the revised
        # field owner keeps the field.
        incompatible = emit_incompatible(scratch)
        check_accepted(args.controller, incompatible, failures)
        broken, broken_url, preamble = pair.spawn_peer(
            args.controller,
            incompatible,
            args.dt,
            rig.plant_addr,
            revised_url.removeprefix("http://"),
            {},
            pair_token=pair.PAIR_TOKEN,
            revised=True,
        )
        if broken_url is None:
            raise Abort(
                "the incompatible peer exited at startup: "
                f"{'; '.join(preamble) or 'no diagnostic'}"
            )
        for _ in range(WINDOW_TICKS):
            pair.scan(broken_url, failures)
        broken_role = pair.get(f"{broken_url}/role", "GET /role", failures)
        broken_sync = broken_role.get("sync")
        detail_text = (
            broken_sync.get("degraded", {}).get("detail", "")
            if isinstance(broken_sync, dict)
            else ""
        )
        if broken_role.get("role") != "standby" or not (
            isinstance(broken_sync, dict) and "degraded" in broken_sync
        ):
            failures.append(
                "the incompatible peer left the refused state — GET "
                f"/role answers {broken_role}, expected standby + "
                "degraded"
            )
            raise Abort
        if "240" not in detail_text or "int" not in detail_text:
            failures.append(
                "the incompatible crossing's diagnostic does not name "
                f"the retyped point and kind: {detail_text}"
            )
            raise Abort
        status, refusal = pair.request(f"{broken_url}/promote", {})
        refused_sync = (
            refusal.get("not_converged", {}).get("sync")
            if isinstance(refusal, dict)
            else None
        )
        if status != 409 or not (
            isinstance(refused_sync, dict) and "degraded" in refused_sync
        ):
            failures.append(
                f"POST /promote on the incompatible peer answered "
                f"{status} {refusal}, expected 409 not_converged "
                "carrying the degraded crossing"
            )
            raise Abort
        owner_role = pair.get(
            f"{revised_url}/role", "GET /role", failures
        )
        if owner_role.get("role") != "active":
            failures.append(
                "the revised field owner lost the field across the "
                f"incompatible peer's attempt: {owner_role}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "refusal",
                "detail": detail_text,
                "status": status,
                "refusal": refusal,
            }
        )
        evidence["refused_detail"] = detail_text
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        pair.stop(broken)
        pair.stop(revised)
        if rig is not None:
            rig.close()
        shutil.rmtree(scratch, ignore_errors=True)
    return digest_entries, evidence, failures


def emit_incompatible(scratch):
    """The incompatible revision, derived from the composed revision
    2 — the doctoring the refusal leg needs, kept beside the
    emit so the checked-in model stays pristine."""
    with open(os.path.join(scratch, "model-revision-2.json")) as handle:
        document = json.load(handle)
    points = {point["id"]: point for point in document["io_points"]}
    held = points.get(HELD_POINT)
    if held is None or held.get("value_type") != "bool":
        raise Abort(
            f"the revision no longer declares the held bool point "
            f"{HELD_POINT}"
        )
    held["value_type"] = "int"
    held["initial"] = {"int": 0}
    document["io_points"].append(
        {
            "id": SPARE_POINT,
            "direction": "in",
            "value_type": "bool",
            "writable": False,
            "initial": {"bool": False},
        }
    )
    document["signals"].append(
        {
            "id": 10000 + SPARE_POINT,
            "name": "exercise-run-spare",
            "source": SPARE_POINT,
            "unit": "",
            "description": "Spare run input the incompatible revision repoints",
            "group": "program",
        }
    )
    rewired = 0
    for connection in document["connections"]:
        if connection.get("from", {}).get("point") == HELD_POINT:
            connection["from"]["point"] = SPARE_POINT
            rewired += 1
    if rewired != 1:
        raise Abort(
            f"expected one connection from the held point "
            f"{HELD_POINT}, found {rewired}"
        )
    path = os.path.join(scratch, "model-revision-broken.json")
    with open(path, "w") as handle:
        json.dump(document, handle, indent=2)
    return path


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
        choices=["unarmed-revision", "incompatible-model"],
        help="doctor the roll — launch the revised peer unarmed, or "
        "roll the incompatible revision — the pass must fail naming "
        "what it saw",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = revision_pass(args, args.tamper)
    except Abort as abort:
        for line in abort.args:
            eprint(f"revision-roll: {line}")
        return 1
    for failure in failures:
        eprint(f"revision-roll: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"revision-roll: the {args.tamper} case passed silently "
                "— the leg never noticed the refused roll"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"revision-roll-digest {digest} — crossed to "
        f"{evidence['initialized']} with {evidence['carried']} carried, "
        f"handover at tick {evidence['handover']}, incompatible refused "
        f"({evidence['refused_detail']})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
