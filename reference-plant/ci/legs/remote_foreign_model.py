#!/usr/bin/env python3
"""The remote foreign-model refusal leg for the reference plant — the
consumer-boundary proof that #1302's remote-attachment correspondence
contract holds on the manifest-declared deployment, not just on the
platform's own two-fixture rig (WW-ENG-003, WW-LCM-001 — the
`remote-born-active-claims-foreign-model-field` finding).

The platform test launches a `--remote` born-active carrying the pump
station's model against a plant server serving the dosing skid: the
declared-point correspondence probe — every channel-bound `io_point`
the model declares must answer on the plant with the declared value
kind before the field's write-ownership claim may land — refuses the
launch outright, and a plant that cannot answer at launch keeps the
pending run's claim from ever landing on a field that thaws foreign.
The consumer tree ships only its own model, so this leg stages the
same miswiring a customer deployment produces — a manifest's plant
endpoint pointing at the wrong container or a stale address — from
the other side: the deployment serves its own plant, and the foreign
model is the emitted document doctored in place, one channel-bound
point retyped (point and bound channel together, so the document
still loads) into a kind the served plant does not answer for that
id. A labeled third controller declaring the doctored document is
launched `--remote` at the deployed pair's live plant endpoint on the
pair's own born-active launch shape. The run:

- converges the manifest-declared pair to `tracking` first — the same
  wiring `ci/legs/pair.py` runs — and captures the field owner's
  claim attribution, both peers' journal positions, and the fencing
  verdict a third-party write meets as the baseline the attempt must
  leave untouched;
- emits the doctored document into the leg's scratch and launches the
  foreign controller: its startup claim must be refused by name —
  the launch either exits nonzero carrying the correspondence verdict
  (`serves io point … the model declares …` / `does not serve io
  point`) or stands pending carrying the named deferred-probe
  mismatch, never serving `active`, never claiming the field;
- holds the rightful pair undisturbed across the attempt: the field
  owner keeps scanning at the driven cadence, the tracking peer's
  image stays identical, neither peer's journal gains a fencing,
  role, divergence, or restart record, and a third-party write probe
  still meets the standing claim's fencing naming the incumbent's
  owner token;
- then runs the positive control: the same third launch on the
  *unmodified* model — `--peer` naming the field owner so the
  arbitration verdict is served configured — claims normally: the
  correspondence gate passes, the conditional grant meets the live
  incumbent's refusal, and the run rejoins the declared pair as a
  tracking standby, proving the refusal names the model divergence
  rather than some ambient claim defect;
- stops both extra members and leaves the declared pair on its
  launch roles for the legs behind this one.

The contract postdates releases the manifest may pin: a pinned
release predating #1302's gate lets the foreign-model launch reach
the claim ask — the incumbent's arbitration refusal or a standing
claim are both the pre-contract surface — so any disposition other
than the named correspondence refusal reports `inconclusive`, never
a product failure. The v0.6.0 artifact set predates the contract.

Usage:

    remote_foreign_model.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `remote-foreign-model-digest <sha256>` line prints —
the check runs two passes and compares them
(`remote-foreign-model-nondeterministic`). A contract violation
reports `remote-foreign-model: …` lines on stderr and exits 1 — the
check's `foreign-model-claim-failed`. `--tamper expect-foreign-claim`
doctors the leg's own expectation — asserting the mismatched launch
claimed the field, a disposition no honest release produces — so the
check proves the leg's refusal classification fires rather than
passing an unexercised contract.
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import born_active_failure
import claim_reclaim
import driver_recovery
import failover
import pair
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored case.
# The doctored case: a leg asserting the mismatched launch claimed
# the field — an impossible disposition under the contract — must
# surface the named diagnostic on the honest refusal rather than
# passing an unexercised contract.
LEG = {
    "order": 700,
    "title": "the remote foreign-model refusal leg",
    "passes": "foreign-model-claim",
    "failed": "foreign-model-claim-failed",
    "tampers": [
        {
            "name": "expect-foreign-claim",
            "passed": "an expect-foreign-claim case passed the remote-foreign-model leg",
            "missed": "the expect-foreign-claim case did not report its named diagnostic",
            "evidence": [
                "the doctored expectation wanted the foreign-model launch to claim the field"
            ],
        },
    ],
}


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The pinned release predates the remote-attachment
    correspondence contract — a foreign-model launch reaching the
    claim ask at all is the pre-#1302 behavior, whether the incumbent's
    arbitration then refuses it or the claim lands — or the served
    substrate the leg's attribution reads is absent: the run
    classifies inconclusive, never a product failure. The first arg
    is the stable reason the `inconclusive` digest line prints — two
    identical passes must share it — and the optional second arg the
    run's own evidence, reported on stderr only."""


# The driven ticks each phase runs: the observation window over the
# refused foreign launch, the rejoin bound the control member
# converges `tracking` inside, and the settle ticks the restore
# proves launch roles on.
WINDOW_TICKS = 4
REJOIN_SCANS = 6
SETTLE_TICKS = 3

# The named verdict the contract owes — the launch-time probe's own
# wording and its deferred re-run's: the served kind against the
# declared one, or the id the plant does not serve at all.
MISMATCH_MARKS = ("does not serve io point", "serves io point")

# The startup-exit vocabulary a pre-contract release's refusal names
# — the incumbent's arbitration verdict, a transport refusal, the
# rejoin remedy, an unrecognized flag — every one an answered launch
# that never met the correspondence probe: inconclusive, never a
# defect.
PREDATING_WORDS = (
    "claim",
    "peer",
    "cannot connect",
    "unrecognized",
    "unknown option",
    "rejoin",
)

# The doctored expectation's stable evidence prefix — every failure
# the tampered pass records carries it.
TAMPER_EVIDENCE = (
    "the doctored expectation wanted the foreign-model launch to "
    "claim the field"
)

# The journal event kinds the deployed pair's record must not gain
# while the foreign launch's attempt is refused — role, fencing,
# divergence, restart, command, and run-boundary records would be the
# disturbance a leaked claim leaves. Ordinary scan transitions are
# the run's own record.
DISTURBANCE_EVENTS = {
    "role_changed",
    "field_claim_lost",
    "divergence_detected",
    "divergence_resolved",
    "source_restarted",
    "reinitialized",
    "command_settled",
    "run_boundary",
}

# The kind each eligible point is retyped to — the doctored
# declaration must differ from the kind the served plant answers.
RETYPED = {"bool": "float", "float": "bool", "int": "bool"}


def correspondence_line(lines):
    """The startup log line naming the declared-point correspondence
    verdict — the kind mismatch or the unserved id — or None where no
    line carries it."""
    for line in lines:
        if any(mark in line for mark in MISMATCH_MARKS):
            return line
    return None


def foreign_model_document(model_path, scratch):
    """The foreign declared model: the emitted document with one
    channel-bound point's declared kind retyped — the point and its
    bound channel's declaration carried together so the doctored
    document still loads, while the plant serving the undoctored
    model answers the id with the original kind. The lever picks a
    point whose kind can flip without disturbing the rest of the
    document's declaration: bound by no other point, referenced by no
    connection endpoint, and unjournaled — a `journaled` point
    retyped to float would fail its own load gate. Written into the
    leg's scratch directory; the checked-in model stays pristine.
    Returns `(path, point_id, declared_kind, served_kind)`."""
    with open(model_path) as handle:
        document = json.load(handle)
    connected = {
        end["point"]
        for connection in document["connections"]
        for end in (connection["from"], connection["to"])
        if isinstance(end, dict) and "point" in end
    }
    bindings = {}
    for point in document["io_points"]:
        channel = point.get("channel")
        if channel is not None:
            bindings.setdefault(
                (channel["device"], channel["name"]), []
            ).append(point)
    for point in document["io_points"]:
        channel = point.get("channel")
        if (
            channel is None
            or point.get("journaled")
            or point["id"] in connected
            or len(bindings[(channel["device"], channel["name"])]) != 1
        ):
            continue
        retyped = RETYPED.get(point["value_type"])
        if retyped is None:
            continue
        device = next(
            entry
            for entry in document["devices"]
            if entry["id"] == channel["device"]
        )
        served_kind = point["value_type"]
        point["value_type"] = retyped
        device["channels"][channel["name"]]["value_type"] = retyped
        path = os.path.join(scratch, "foreign-model.json")
        with open(path, "w") as handle:
            json.dump(document, handle, indent=2)
        return path, point["id"], retyped, served_kind
    raise Abort(
        "the emitted model declares no retypable channel-bound point "
        "— the leg has no foreign-model lever"
    )


def classify_exit(preamble):
    """The launched controller exited before reporting a listener.
    Returns the normalized verdict: "refused" when the startup log
    names the declared-point correspondence verdict, "predating" when
    it names a refusal vocabulary a release without the gate would
    produce — the incumbent's arbitration, the transport, the
    invocation itself — and "silent" when nothing names why."""
    named = correspondence_line(preamble)
    if named is not None:
        return "refused", named
    joined = " ".join(preamble)
    if any(word in joined for word in PREDATING_WORDS):
        return "predating", "; ".join(preamble[-2:]) or "no diagnostic"
    return "silent", "; ".join(preamble) or "no diagnostic"


def trailing_stderr(process):
    """The rest of an exited member's stderr — the lines after
    `listening on` a refused-pending run's teardown verdict or a
    mid-window exit writes. Only read once the process is known
    dead."""
    try:
        return [line.strip() for line in process.stderr if line.strip()]
    except Exception:
        return []


def remote_foreign_model_pass(args, tamper):
    """The remote foreign-model refusal run: converge the deployed
    pair, launch the doctored-model controller at its live plant
    endpoint, assert the named refusal while the pair's ownership and
    scan cadence hold, then the unmodified-model control and the
    restore. Returns `(digest_entries, evidence, failures)`; raises
    `Inconclusive` where the pinned release predates the
    contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "remote-foreign-model leg has nothing to exercise"
        )
    _manifest, duty_decl, _standby_decl = declared
    digest_entries, evidence, failures = [], {}, []
    scratch = tempfile.mkdtemp(prefix="dcs-foreign-model-")
    rig = probe_io = None
    members = []
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        probe_io = simulate.PlantClient(rig.plant_addr)

        # Phase 1 — convergence and the baseline the attempt must
        # leave untouched: the incumbent's owner token, both peers'
        # journal positions, and the standing fencing verdict a
        # third-party write meets. A startup log carrying no claim
        # line is the release predating the contract's substrate.
        converged = rig.converge(failures)
        incumbent_token = failover.owner_token(rig.duty_preamble)
        if incumbent_token is None:
            raise Inconclusive(
                "the field owner's startup log carries no "
                "write-ownership claim line — the pinned release "
                "predates the startup-claim record the leg's "
                "attribution reads"
            )
        with open(args.model) as handle:
            model = json.load(handle)
        points = failover.signal_points(model)
        if points is None:
            raise Abort(
                "the emitted model declares no p101-cmd/level-primary "
                "signal points — the leg has no field probe to run"
            )
        held = failover.field_read(probe_io, points["cmd"], failures)[
            "value"
        ]
        verdict = failover.foreign_probe(
            probe_io, points["cmd"], held
        )
        if not claim_reclaim.mutation_fenced(verdict):
            raise Inconclusive(
                "a third-party write met no fencing verdict — the "
                "pinned release predates the claim-arbitration "
                "substrate the leg's undisturbed proof reads",
                f"the write probe answered {verdict}",
            )
        if claim_reclaim.verdict_owner(verdict) != incumbent_token:
            failures.append(
                "the standing fencing verdict names owner "
                f"{claim_reclaim.verdict_owner(verdict)}, not the "
                f"incumbent's token {incumbent_token}"
            )
            raise Abort
        duty_journal0 = pair.get(
            f"{duty_url}/journal", "GET /journal", failures
        )
        standby_journal0 = pair.get(
            f"{standby_url}/journal", "GET /journal", failures
        )
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )

        # Phase 2 — the foreign launch: the doctored emitted model
        # declared against the deployed pair's live plant endpoint on
        # the pair's own born-active launch shape — the miswired
        # attachment a customer manifest produces when a plant
        # endpoint points at the wrong container.
        doctored, point_id, declared_kind, served_kind = (
            foreign_model_document(args.model, scratch)
        )
        evidence["doctored"] = {
            "point": point_id,
            "declared_kind": declared_kind,
            "served_kind": served_kind,
        }
        foreign, foreign_url, foreign_preamble = pair.spawn_peer(
            args.controller,
            doctored,
            args.dt,
            rig.plant_addr,
            None,
            {},
        )

        window_ticks = []
        if foreign_url is None:
            # The contract's eager half: the probe answered inside
            # startup, so the miswired --remote is an assembly
            # failure, not a running owner.
            disposition, detail = classify_exit(foreign_preamble)
            if tamper is not None:
                failures.append(
                    f"{TAMPER_EVIDENCE} — the launch exited at "
                    f"startup ({disposition}): {detail}"
                )
            elif disposition == "refused":
                evidence["refusal"] = detail
            elif disposition == "predating":
                raise Inconclusive(
                    "the pinned release predates the remote "
                    "correspondence contract — the foreign-model "
                    "launch exited naming a refusal other than the "
                    "declared-point verdict",
                    f"the exit reported: {detail}",
                )
            else:
                failures.append(
                    "the foreign-model launch exited at startup "
                    "without naming the correspondence verdict: "
                    f"{detail}"
                )
        else:
            # The deferred half the contract also allows: a launch
            # whose claim ask meets the mismatch stands pending and
            # never lets the claim land. Watch it across the window —
            # a run that goes promoting/active claimed the foreign
            # field, the pre-gate release's shape; one that stays
            # standby owes the named pending-with-mismatch verdict.
            members.append(foreign)
            pending_verdict = None
            for _ in range(WINDOW_TICKS):
                if foreign.poll() is not None:
                    # An exit mid-window: classify on the whole
                    # startup record, trailing lines included.
                    disposition, detail = classify_exit(
                        foreign_preamble + trailing_stderr(foreign)
                    )
                    pending_verdict = disposition
                    break
                pair.scan(foreign_url, failures)
                report = pair.get(
                    f"{foreign_url}/role", "GET /role", failures
                )
                if report.get("role") in ("promoting", "active"):
                    pending_verdict = "claimed"
                    break
                tracked, owner = rig.tick(
                    standby_url, duty_url, failures
                )
                window_ticks.append(owner["tick"])
            if tamper is not None:
                failures.append(
                    f"{TAMPER_EVIDENCE} — the launch served "
                    f"{pending_verdict or 'pending'} at "
                    f"{foreign_url}"
                )
            elif pending_verdict == "claimed":
                raise Inconclusive(
                    "the pinned release predates the remote "
                    "correspondence contract — the foreign-model "
                    "launch claimed the field and serves active",
                    f"GET /role answered {report}",
                )
            elif pending_verdict in ("predating", "silent"):
                raise Inconclusive(
                    "the pinned release predates the remote "
                    "correspondence contract — the foreign-model "
                    "launch exited mid-run naming no declared-point "
                    "verdict",
                    f"the exit reported: {detail}",
                )
            elif pending_verdict == "refused":
                evidence["refusal"] = detail
                evidence["pending_verdict"] = "refused-in-window"
            else:
                named = correspondence_line(foreign_preamble)
                if named is None:
                    failures.append(
                        "the foreign-model launch holds pending "
                        "without naming the mismatch — the deferred "
                        "probe's own detail is the pending run's "
                        f"evidence: {foreign_preamble}"
                    )
                else:
                    evidence["refusal"] = named
                    evidence["pending_verdict"] = "pending-with-mismatch"

        # Phase 3 — the pair undisturbed: the field owner's tick
        # advanced across the window, launch roles hold, neither
        # peer's journal gained a disturbance record, and the field's
        # fencing verdict still names the incumbent's token — the
        # refused attachment never claimed and never wrote.
        for _ in range(SETTLE_TICKS):
            tracked, owner = rig.tick(standby_url, duty_url, failures)
            window_ticks.append(owner["tick"])
        if not driver_recovery.roles_hold(
            rig, failures, "through the foreign-model attempt"
        ):
            raise Abort
        duty_journal1 = pair.get(
            f"{duty_url}/journal", "GET /journal", failures
        )
        standby_journal1 = pair.get(
            f"{standby_url}/journal", "GET /journal", failures
        )
        for peer_name, before, after in (
            (duty_decl["name"], duty_journal0, duty_journal1),
            ("the declared standby", standby_journal0, standby_journal1),
        ):
            leaked = [
                entry
                for entry in after[len(before):]
                if DISTURBANCE_EVENTS & set(entry.get("event", {}))
            ]
            if leaked:
                failures.append(
                    f"{peer_name}'s journal gained disturbance "
                    f"records during the foreign-model attempt: "
                    f"{leaked}"
                )
                raise Abort
        verdict = failover.foreign_probe(
            probe_io, points["cmd"], held
        )
        if claim_reclaim.verdict_owner(verdict) != incumbent_token:
            failures.append(
                "the field's fencing verdict moved off the "
                f"incumbent's token during the foreign-model "
                f"attempt: {verdict}"
            )
            raise Abort
        if not failures:
            digest_entries.append(
                {
                    "phase": "foreign-launch",
                    "point": point_id,
                    "declared_kind": declared_kind,
                    "served_kind": served_kind,
                    "verdict": evidence.get("pending_verdict", "refused-at-startup"),
                    "window_ticks": window_ticks,
                }
            )

        # Phase 4 — the positive control: the same launch on the
        # unmodified model, `--peer` naming the field owner. The
        # correspondence gate passes, the conditional grant meets the
        # live incumbent's refusal, and the run rejoins the declared
        # pair as its tracking standby — the claim path itself is
        # normal; only the foreign document was refused.
        control, control_url, control_preamble = (
            born_active_failure.spawn_born_active(
                args,
                rig.plant_addr,
                {},
                peer=duty_url.removeprefix("http://"),
            )
        )
        if control_url is None:
            joined = " ".join(control_preamble)
            if any(word in joined for word in PREDATING_WORDS):
                raise Inconclusive(
                    "the pinned release predates the refused-claim "
                    "rejoin contract the positive control relies on "
                    "— the unmodified launch exited at startup",
                    "; ".join(control_preamble[-2:])
                    or "no diagnostic",
                )
            failures.append(
                "the unmodified control launch exited at startup "
                "without a claim-arbitration verdict: "
                f"{'; '.join(control_preamble) or 'no diagnostic'}"
            )
            raise Abort
        members.append(control)
        if not (
            any(
                "rejoined as standby" in line
                for line in control_preamble
            )
            and any(
                "a live peer holds the field's write-ownership claim"
                in line
                for line in control_preamble
            )
        ):
            failures.append(
                "the control launch's startup log carries neither "
                "the incumbent's refusal nor the declared-pair "
                f"rejoin: {control_preamble}"
            )
        control_ticks = []
        converged_sync = None
        for _ in range(REJOIN_SCANS):
            snapshot = pair.scan(control_url, failures)
            control_ticks.append(snapshot["tick"])
            report = pair.get(
                f"{control_url}/role", "GET /role", failures
            )
            if report.get("role") != "standby":
                failures.append(
                    "the control member left standby — GET /role "
                    f"answers {report}"
                )
                raise Abort
            sync = report.get("sync")
            if isinstance(sync, dict) and (
                "tracking" in sync or "reinitialized" in sync
            ):
                converged_sync = sync
                break
            rig.tick(standby_url, duty_url, failures)
        if converged_sync is None:
            failures.append(
                "the unmodified control never rejoined the declared "
                f"pair — its sync stayed {report.get('sync')}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "control",
                "verdict": "claim-arbitrated-rejoined",
                "ticks": control_ticks,
                "sync": next(iter(converged_sync)),
            }
        )

        # Phase 5 — the restore: every extra member stopped, the
        # declared pair resting on its launch roles for the legs
        # behind this one.
        for process in members:
            pair.stop(process)
        members.clear()
        for _ in range(SETTLE_TICKS):
            tracked, owner = rig.tick(standby_url, duty_url, failures)
        if not driver_recovery.roles_hold(
            rig, failures, "after the foreign-model attempt"
        ):
            raise Abort
        digest_entries.append(
            {
                "phase": "restore",
                "duty": "active",
                "standby": "tracking",
            }
        )
        evidence["final_tick"] = owner["tick"]
    except Inconclusive:
        raise
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        for process in members:
            pair.stop(process)
        if probe_io is not None:
            probe_io.close()
        if rig is not None:
            rig.close()
        shutil.rmtree(scratch, ignore_errors=True)
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
        choices=["expect-foreign-claim"],
        help="doctor the leg's own expectation — the mismatched "
        "launch must have claimed the field, a disposition no "
        "release honoring the contract produces — so the pass must "
        "fail naming the verdict it observed",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = remote_foreign_model_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                f"remote-foreign-model: {TAMPER_EVIDENCE} — an "
                "inconclusive run offers the doctored case no "
                "evidence"
            )
            return 1
        reason = inconclusive.args[0]
        detail = inconclusive.args[-1]
        eprint(f"remote-foreign-model: inconclusive — {detail}")
        print(f"remote-foreign-model-digest inconclusive — {reason}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"remote-foreign-model: {line}")
        return 1
    for failure in failures:
        eprint(f"remote-foreign-model: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"remote-foreign-model: the {args.tamper} case "
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
        f"remote-foreign-model-digest {digest} — the declared pair "
        "converged, the foreign-model launch was refused by name at "
        "startup with the pair's ownership and cadence undisturbed, "
        "the unmodified control claimed and scanned normally, and "
        f"launch roles held at tick {evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
