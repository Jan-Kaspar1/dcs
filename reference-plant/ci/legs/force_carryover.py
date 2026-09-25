#!/usr/bin/env python3
"""The force-carryover leg for the reference plant — the consumer-side
proof that a standing operator force survives a promotion on the
deployed redundant pair (WW-ENG-003, WW-LCM-001 — decision 21's
checkpoint-carried force set).

The pair leg (`ci/legs/pair.py`) proves the declared pair runs and switches
bumplessly; the takeover leg proves the receipted write seam. This leg
proves the run-state carry the continuity clause requires for forcing:
a force set while the pair tracks must ride the checkpoint — the
tracking peer's scans run adopted state, so its snapshot already
carries the same force — and must still stand on the peer the
promotion makes field owner. A promotion that silently released the
operator's forcing is exactly the misbehavior the continuity clause
forbids.

On the target: the emitted model marks only *internal* `In` points
writable — the operator seam (alarm acks, per-pump `mode`/`hand`/`oos`,
the exercise `run` request); no field `In` point is declared writable,
so the honest force target here is internal — which the released
contract accepts: while forced, the input image holds the substituted
value stamped `Uncertain(Substituted)`; on release, the held internal
sample is re-stamped `Good`. The leg resolves `p101-hand` through the
signal index — the operator's hand run request, gated by the
manual-mode AND while the pump stands in auto, so forcing it perturbs
nothing downstream — verifies the model declares it writable, then:

- records the point's unforced sample — the control proving the
  unforced value is what would otherwise have served — and asserts it
  differs from the forced value, else the substitution proves nothing;
- submits `force_point` through `POST /command` on the active's
  monitor — the leg's chosen receipted path — asserting the `accepted`
  submission, the `applied` settlement identical in both peers'
  adopted log, the snapshot's `forces` entry naming the point, and the
  forced value stamped `Uncertain(Substituted)` on both peers'
  snapshots;
- issues the documented switch — `POST /demote` on the field owner,
  `POST /promote` on the converged standby — then drives scans while
  asserting on the promoted peer that `forces` still names the point
  and the sample stays the forced value at substituted quality;
- submits `unforce_point` on the new active, asserting the `accepted`
  submission settling `applied`, the emptied `forces` list, and the
  point resuming its unforced serve — for an internal target the
  held-value rule resumes with the force's last stamp re-stamped
  `Good`, the observable state a same-value `WriteValue` produces (a
  field point would resume the driver's read — the emitted model
  declares no writable field `In` point);
- restores the pair's roles — `POST /demote` on the new owner, `POST
  /promote` on the reconverged peer — leaving the manifest-declared
  duty controller `active` and its standby `tracking` again.

Usage:

    force_carryover.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `force-digest <sha256>` line prints — the check runs
two passes and compares them (`force-carryover-nondeterministic`). A
contract violation reports `force-carryover: …` lines on stderr and
exits 1 — the check's `force-carryover-failed`. `--tamper
expect-unforced` doctors the post-promotion expectation to the point's
unforced value — a genuine carryover must fail it.
"""

import argparse
import hashlib
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import pair
import simulate
import takeover


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored case: a leg asserting the unforced value after
# promotion must surface the named diagnostic — the force
# rides the checkpoint, never a silently released pass.
LEG = {
    "order": 70,
    "title": "the force-carryover leg",
    "passes": "force-carryover",
    "tampers": [
        {
            "name": "expect-unforced",
            "passed": "a doctored unforced expectation passed the carryover leg",
            "missed": "the expect-unforced case did not report its named diagnostic",
            "evidence": ["expected the unforced value"],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The driven ticks each phase runs — the post-switch scans the force
# must stand across. The actor the leg's receipted submissions
# declare.
CARRY_TICKS = 4
RESTORE_TICKS = 4
ACTOR = "ci-force"

# The substituted stamp a forced sample carries — the served quality
# encoding of `Uncertain(Substituted)`.
SUBSTITUTED = {"uncertain": "substituted"}

# The signal resolving the force target: the operator's hand run
# request — a declared writable internal `In` point consumed only
# through the manual-mode AND, inert downstream while the pump stands
# in auto.
FORCE_SIGNAL = "p101-hand"


def force_target(model):
    """The leg's force point — `FORCE_SIGNAL`'s declared source when
    the emitted model marks that source a writable `In` point; None
    otherwise. A signal's `source` is the point it names; the
    lowest-signal-id-wins rule the served index applies."""
    by_name = {}
    for signal in model.get("signals", []):
        current = by_name.get(signal["name"])
        if current is None or signal["id"] < current:
            by_name[signal["name"]] = signal["source"]
    point = by_name.get(FORCE_SIGNAL)
    if point is None:
        return None
    for io_point in model.get("io_points", []):
        if io_point["id"] == point:
            if io_point.get("direction") == "in" and io_point.get("writable"):
                return point
            return None
    return None


def point_sample(snapshot, point):
    """The point's served sample — `{value, quality, tick}` — or
    None."""
    for entry in snapshot["points"]:
        if entry["point"] == point:
            return entry.get("sample")
    raise KeyError(point)


def force_entry(forces, point):
    """The snapshot `forces` entry naming `point`, or None."""
    return next(
        (entry for entry in forces if entry.get("point") == point), None
    )


def submit(url, command, failures):
    """POST one receipted command to the active's `/command` and assert
    the `accepted` submission — returns the receipt."""
    status, receipt = pair.request(
        f"{url}/command", {"command": command, "actor": ACTOR}
    )
    if status != 200 or simulate.receipt_outcome(receipt) != "accepted":
        failures.append(
            f"the receipted command {command} answered {status} "
            f"{receipt}, expected an accepted receipt"
        )
        raise Abort
    return receipt


def assert_force(snapshot, point, forced, what, failures):
    """The force-badge assertions on one snapshot: `forces` names the
    point at the forced value, and the point's sample reads the forced
    value stamped `Uncertain(Substituted)`. Returns the fetched sample
    for the digest, or None after recording a failure."""
    entry = force_entry(snapshot.get("forces", []), point)
    if entry is None or entry.get("value") != forced:
        failures.append(
            f"{what} forces {snapshot.get('forces')} do not name "
            f"point {point} at {forced}"
        )
        return None
    found = point_sample(snapshot, point)
    if found is None or found["value"] != forced or found["quality"] != SUBSTITUTED:
        failures.append(
            f"{what} forced point reads {found}, expected {forced} "
            f"at {SUBSTITUTED}"
        )
        return None
    return found


def carryover_pass(args, tamper):
    """The carryover run: converge, control, force, switch, carry,
    release, restore. Returns `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the force leg has "
            "nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    with open(args.model) as handle:
        model = json.load(handle)
    point = force_target(model)
    if point is None:
        raise Abort(
            f"the emitted model declares no writable In point at "
            f"{FORCE_SIGNAL} — the force leg has nothing to exercise"
        )
    forced = {"bool": True}
    force_command = {
        "force_point": {"point": point, "kind": "bool", "value": forced}
    }
    unforce_command = {"unforce_point": {"point": point}}

    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared, tamper)
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

        # Phase 2 — the control and the receipted force. The unforced
        # sample is the value that would otherwise have served; it must
        # differ from the forced value or the substitution proves
        # nothing.
        control = point_sample(owner, point)
        if control is None:
            failures.append(
                f"the force target point {point} serves no sample"
            )
            raise Abort
        if control["quality"] != "good":
            failures.append(
                f"the force target point {point} serves {control} "
                "before the force — the unforced control must read good"
            )
            raise Abort
        if control["value"] == forced:
            failures.append(
                f"the force target point {point} already reads {forced} "
                "unforced — forcing it proves nothing"
            )
            raise Abort
        receipt = submit(duty_url, force_command, failures)
        tracked, owner = pair.tick(standby_url, duty_url, failures)
        active_sample = assert_force(
            owner, point, forced, "the active's", failures
        )
        tracked_sample = assert_force(
            tracked, point, forced, "the tracking peer's", failures
        )
        if failures:
            raise Abort
        receipts_duty = pair.get(f"{duty_url}/receipts", "GET /receipts", failures)
        receipts_standby = pair.get(
            f"{standby_url}/receipts", "GET /receipts", failures
        )
        if receipts_duty != receipts_standby:
            failures.append(
                "the peers' receipt logs diverged — the adopted audit "
                "is not one log"
            )
            raise Abort
        if not takeover.settled(receipts_duty, force_command):
            failures.append(
                "the force never settled applied into the adopted "
                "receipt log"
            )
            raise Abort
        evidence["forced_at"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "force",
                "point": point,
                "control": control,
                "receipt": receipt,
                "active": {"forces": owner["forces"], "sample": active_sample},
                "tracked": {"forces": tracked["forces"], "sample": tracked_sample},
            }
        )

        # Phase 3 — the documented switch: demote the field owner,
        # promote the converged standby, each answered by its
        # RoleReport.
        demote = rig.demote(duty_url, failures)
        promote = rig.promote(standby_url, failures)
        evidence["switched_at"] = demote["tick"]
        digest_entries.append(
            {"phase": "switch", "demote": demote, "promote": promote}
        )

        # Phase 4 — the carry: the promoted peer must keep the force
        # the checkpoint carried — `forces` still names the point and
        # the sample stays the forced value at substituted quality
        # while scans advance. A dropped or silently re-substituted
        # force fails here.
        ticks = []
        carried_sample = None
        for _ in range(CARRY_TICKS):
            tracked, owner = pair.tick(duty_url, standby_url, failures)
            entry = force_entry(owner.get("forces", []), point)
            if entry is None or entry.get("value") != forced:
                failures.append(
                    f"the promoted peer's forces {owner.get('forces')} "
                    f"no longer name point {point} at {forced} — the "
                    "force did not ride the promotion's adopted state"
                )
                raise Abort
            sample = point_sample(owner, point)
            if tamper == "expect-unforced":
                want_value, want_quality = control["value"], "good"
            else:
                want_value, want_quality = forced, SUBSTITUTED
            if (
                sample is None
                or sample["value"] != want_value
                or sample["quality"] != want_quality
            ):
                if tamper == "expect-unforced":
                    failures.append(
                        f"the promoted peer's forced point reads "
                        f"{sample}, expected the unforced value "
                        f"{want_value} at good"
                    )
                else:
                    failures.append(
                        f"the promoted peer's forced point reads "
                        f"{sample}, expected {forced} at {SUBSTITUTED} "
                        "after promotion"
                    )
                raise Abort
            carried_sample = sample
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
        evidence["carried_through"] = ticks[-1]
        digest_entries.append(
            {
                "phase": "carry",
                "ticks": ticks,
                "forces": owner["forces"],
                "sample": carried_sample,
                "duty_role": duty_role,
                "standby_role": standby_role,
            }
        )

        # Phase 5 — the release on the new active: `unforce_point`
        # through the same receipted path settles `applied`, the
        # `forces` list empties, and the point resumes its unforced
        # serve. For an internal target the held-value rule resumes
        # with the force's last stamp re-stamped `Good` — the
        # observable state a same-value `WriteValue` produces; a field
        # point would resume the driver's read.
        release = submit(standby_url, unforce_command, failures)
        tracked, owner = pair.tick(duty_url, standby_url, failures)
        if owner.get("forces"):
            failures.append(
                f"the released run still carries forces "
                f"{owner.get('forces')} — the unforce did not empty "
                "the set"
            )
        sample = point_sample(owner, point)
        if (
            sample is None
            or sample["value"] != forced
            or sample["quality"] != "good"
        ):
            failures.append(
                f"the released point reads {sample}, expected the held "
                f"value {forced} re-stamped good"
            )
        receipts_standby = pair.get(
            f"{standby_url}/receipts", "GET /receipts", failures
        )
        receipts_duty = pair.get(f"{duty_url}/receipts", "GET /receipts", failures)
        if receipts_duty != receipts_standby:
            failures.append(
                "the peers' receipt logs diverged across the switch — "
                "the adopted audit is not one log"
            )
        if not takeover.settled(receipts_standby, unforce_command):
            failures.append(
                "the unforce never settled applied into the adopted "
                "receipt log"
            )
        if failures:
            raise Abort
        evidence["released_at"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "release",
                "receipt": release,
                "forces": owner["forces"],
                "sample": sample,
            }
        )

        # Phase 6 — the roles restore: demote the new owner, promote
        # the reconverged peer, and drive the pair back to the
        # manifest's declared arrangement — the duty controller
        # `active`, its standby `tracking`.
        demote = rig.demote(standby_url, failures, "the new field owner")
        promote = rig.promote(duty_url, failures, "the reconverged peer")
        ticks = []
        for _ in range(RESTORE_TICKS):
            tracked, owner = pair.tick(standby_url, duty_url, failures)
            ticks.append(owner["tick"])
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        standby_role = pair.get(f"{standby_url}/role", "GET /role", failures)
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
        evidence["restored_at"] = ticks[-1]
        digest_entries.append(
            {
                "phase": "restore",
                "demote": demote,
                "promote": promote,
                "ticks": ticks,
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
        choices=["expect-unforced"],
        help="doctor the post-promotion expectation to the point's "
        "unforced value — the pass must fail naming the force's "
        "carried reading",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = carryover_pass(args, args.tamper)
    except Abort as abort:
        for line in abort.args:
            eprint(f"force-carryover: {line}")
        return 1
    for failure in failures:
        eprint(f"force-carryover: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"force-carryover: the {args.tamper} case passed "
                "silently — the leg never noticed the force still "
                "standing after promotion"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"force-digest {digest} — tracking by tick "
        f"{evidence['converged']}, forced at tick "
        f"{evidence['forced_at']}, carried through tick "
        f"{evidence['carried_through']}, released at tick "
        f"{evidence['released_at']}, roles restored at tick "
        f"{evidence['restored_at']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
