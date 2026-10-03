#!/usr/bin/env python3
"""The tune-carryover leg for the reference plant — the consumer-side
proof that a customer's receipted parameter tuning survives a promotion
on the deployed redundant pair (WW-ENG-003, WW-LCM-001's
runtime-tuning-continuity clause).

The pair leg (`ci/legs/pair.py`) proves the declared pair runs and switches
bumplessly; the force-carryover leg proves the continuity clause for
forcing. This leg proves it for parameter tuning — the clause the
lane's parameter-tune-carryover scenario evidences on the platform rig,
unproven until now on a deployment a customer writes: a `set_parameter`
tune settled on the active is run state, so it rides the checkpoint the
tracking standby adopts and must still be live on the peer the
promotion makes field owner. A promotion that silently reverted the
operator's tuning to the model-declared default is exactly the
misbehavior the continuity clause forbids.

On the target: the exercise `sequencer`'s `step_1_out` — a
descriptor-declared sequence parameter the served registry carries as a
writable configuration entry — is the honest choice: the parked table
reports step 1, so the tuned demand lands on the `out` port's next
sample while nothing downstream consumes it. The leg resolves the
component, the parameter, the `out` port's bound point, and the
declared signal sourcing that point out of the emitted model, then:

- records the parameter's served value and the `out` sample — the
  control proving the un-tuned default is what an un-carried tune
  would read — and asserts the served value differs from the tuned
  value, else the carryover proves nothing;
- verifies the served registry declares the point writable — the
  component's interface carrying the parameter's `set_parameter`
  command — then submits the tune through `POST /command` on the
  active's monitor, the leg's chosen receipted path, asserting the
  `accepted` submission, the `applied` settlement identical in both
  peers' adopted log, and the tuned value live on both peers: the
  snapshot's `parameters` report and, the `out` port's bound point
  feeding a declared signal, the signal's reading;
- issues the documented switch — `POST /demote` on the field owner,
  `POST /promote` on the converged standby — then drives scans while
  asserting on the promoted peer that the parameter report and the
  signal's reading still carry the tuned value, and audits the
  promoted peer's served journal: the promotion's `role_changed`
  entries ordered after the tune's `command_settled` entry;
- submits a further `set_parameter` on the promoted peer — a fresh
  receipt, never an adopted echo — asserting the `applied` settlement
  into the adopted log, the retuned value live on the parameter
  report and the signal's reading, and its journaled settlement
  ordered after the promotion;
- restores the pair's roles — `POST /demote` on the new owner, `POST
  /promote` on the reconverged peer — leaving the manifest-declared
  duty controller `active` and its standby `tracking` again.

Usage:

    tune_carryover.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `tune-digest <sha256>` line prints — the check runs two
passes and compares them (`tune-carryover-nondeterministic`). A
contract violation reports `tune-carryover: …` lines on stderr and
exits 1 — the check's `tune-carryover-failed`. `--tamper
expect-original` doctors the post-promotion expectation to the
parameter's original value — a genuine carryover must fail it.
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
# The doctored case: a leg asserting the parameter's original
# value after the tune must surface the named diagnostic — the
# tuned value rides the checkpoint, never a silently reverted
# pass.
LEG = {
    "order": 80,
    "title": "the tune-carryover leg",
    "passes": "tune-carryover",
    "tampers": [
        {
            "name": "expect-original",
            "passed": "a doctored original-value expectation passed the carryover leg",
            "missed": "the expect-original case did not report its named diagnostic",
            "evidence": ["expected the original value"],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The driven ticks each phase runs — the post-switch scans the tune
# must stand across. The actor the leg's receipted submissions
# declare.
CARRY_TICKS = 4
RESTORE_TICKS = 4
ACTOR = "ci-tune"

# The tuned values — finite, inside the descriptor's declared
# finite-float range, and distinct from each other so the
# post-promotion retune's command and receipt differ from the tune's.
TUNED = {"float": 2.5}
RETUNED = {"float": 4.0}

# The leg's tune point: the exercise `sequencer`'s `step_1_out` — the
# parked table's driven demand, a descriptor-declared sequence
# parameter — and the `out` port the value drives.
TUNE_PARAMETER = "step_1_out"
OUT_PORT = "out"


def tune_target(model):
    """The leg's tune point resolved out of the emitted model: the
    first `sequencer` component declaring `TUNE_PARAMETER` a float
    parameter, returning `(component, parameter, point, signal)` —
    `component` the served `<kind>:<id>` name, `point` the `out`
    port's bound point, `signal` the declared signal's name sourcing
    that point (None when the point carries none) — or None when the
    emitted model declares no such tunable."""
    bound = simulate.bound_points(model)
    signals = {}
    for signal in model.get("signals", []):
        current = signals.get(signal["source"])
        if current is None or signal["id"] < current["id"]:
            signals[signal["source"]] = signal
    for component in model.get("components", []):
        declared = component.get("parameters", {}).get(TUNE_PARAMETER)
        if component["kind"] != "sequencer" or "float" not in (declared or {}):
            continue
        point = bound.get((component["id"], OUT_PORT))
        signal = signals.get(point)
        return (
            f"{component['kind']}:{component['id']}",
            TUNE_PARAMETER,
            point,
            signal["name"] if signal else None,
        )
    return None


def parameter_value(snapshot, component, name):
    """The live value a snapshot's `parameters` report carries for one
    descriptor-declared parameter — `{"float": …}` — or None when the
    section or the entry is absent."""
    for entry in snapshot.get("parameters") or []:
        if entry.get("name") == component:
            return (entry.get("values") or {}).get(name)
    return None


def submit(url, command, failures):
    """POST one receipted command to the active's `/command` and assert
    the `accepted` submission — returns the receipt."""
    status, receipt = pair.request(
        f"{url}/command", {"command": command, "actor": ACTOR}
    )
    if status != 200 or simulate.receipt_outcome(receipt) != "accepted":
        failures.append(
            f"the receipted tune {command} answered {status} "
            f"{receipt}, expected an accepted receipt"
        )
        raise Abort
    return receipt


def assert_tuned(snapshot, component, name, value, point, what, failures):
    """The tuned-value assertions on one snapshot: the `parameters`
    report carries `name` at `value`, and the `out` port's bound point
    — the declared signal's source — reads `value`. Returns the
    fetched sample for the digest, or None after recording a
    failure."""
    served = parameter_value(snapshot, component, name)
    if served != value:
        failures.append(
            f"{what} parameter report reads {served} for {component}'s "
            f"{name}, expected {value}"
        )
        return None
    sample = simulate.snapshot_point(snapshot, point)
    if sample != value:
        failures.append(
            f"{what} out point reads {sample}, expected the tuned "
            f"value {value} on the declared signal's source"
        )
        return None
    return sample


def settled_seqs(journal, command):
    """The `seq`s of a served journal's `command_settled` entries
    whose receipt answers `command`."""
    return [
        entry["seq"]
        for entry in journal
        if entry.get("event", {}).get("command_settled", {})
        .get("receipt", {}).get("command") == command
    ]


def promotion_seqs(journal):
    """The `seq`s of a served journal's promotion `role_changed`
    entries — the peer's `standby → promoting → active` record."""
    return [
        entry["seq"]
        for entry in journal
        if entry.get("event", {}).get("role_changed", {}).get("to")
        in ("promoting", "active")
    ]


def identical_receipts(duty_url, standby_url, failures):
    """Both peers' adopted receipt logs, asserted one identical log —
    returns the log."""
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
    return receipts_duty


def tune_pass(args, tamper):
    """The carryover run: converge, control, tune, switch, carry,
    retune, restore. Returns `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the tune leg has "
            "nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    with open(args.model) as handle:
        model = json.load(handle)
    target = tune_target(model)
    if target is None:
        raise Abort(
            "the emitted model declares no sequencer step_1_out "
            "parameter — the tune leg has nothing to exercise"
        )
    component, name, point, signal = target
    tune_command = {
        "set_parameter": {
            "component": component,
            "name": name,
            "value": TUNED,
        }
    }
    retune_command = {
        "set_parameter": {
            "component": component,
            "name": name,
            "value": RETUNED,
        }
    }

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

        # Phase 2 — the control and the receipted tune. The un-tuned
        # served value is what an un-carried tune would read after the
        # switch; it must differ from the tuned value or the carryover
        # proves nothing. The served registry must declare the point
        # writable — the parameter's `set_parameter` command on the
        # component's interface.
        control = parameter_value(owner, component, name)
        control_sample = simulate.snapshot_point(owner, point)
        if control is None or control_sample is None:
            failures.append(
                f"the tune target {component}'s {name} serves no "
                f"parameter value or the out point {point} no sample"
            )
            raise Abort
        if control == TUNED:
            failures.append(
                f"the tune target {component}'s {name} already reads "
                f"{TUNED} — tuning it proves nothing"
            )
            raise Abort
        schema = pair.get(f"{duty_url}/schema", "GET /schema", failures)
        interface = next(
            (
                entry.get("interface", {})
                for entry in schema.get("interfaces", [])
                if entry.get("name") == component
            ),
            {},
        )
        commands = {
            spec.get("name"): spec
            for spec in interface.get("commands", [])
        }
        spec = commands.get(f"set_parameter:{name}")
        if not isinstance(spec, dict) or spec.get("adapted") != (
            "set_parameter"
        ):
            failures.append(
                f"the served registry declares no set_parameter:{name} "
                f"command on {component} — the tune target is not a "
                "writable configuration point"
            )
            raise Abort
        receipt = submit(duty_url, tune_command, failures)
        tracked, owner = pair.tick(standby_url, duty_url, failures)
        active_sample = assert_tuned(
            owner, component, name, TUNED, point, "the active's", failures
        )
        tracked_sample = assert_tuned(
            tracked,
            component,
            name,
            TUNED,
            point,
            "the tracking peer's",
            failures,
        )
        if failures:
            raise Abort
        receipts = identical_receipts(duty_url, standby_url, failures)
        if not takeover.settled(receipts, tune_command):
            failures.append(
                "the tune never settled applied into the adopted "
                "receipt log"
            )
            raise Abort
        evidence["tuned_at"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "tune",
                "component": component,
                "parameter": name,
                "point": point,
                "signal": signal,
                "control": {"value": control, "sample": control_sample},
                "receipt": receipt,
                "active": {
                    "value": parameter_value(owner, component, name),
                    "sample": active_sample,
                },
                "tracked": {
                    "value": parameter_value(tracked, component, name),
                    "sample": tracked_sample,
                },
            }
        )

        # Phase 3 — the documented switch: demote the field owner,
        # promote the converged standby, the shared rig's handover
        # ticks keeping the peers' images identical while the adopted
        # receipt log stays one log.
        switched = rig.switch(
            duty_url, standby_url, failures, audit_receipts=True
        )
        evidence["switched_at"] = switched["demote"]["tick"]
        digest_entries.append(
            {
                "phase": "switch",
                "demote": switched["demote"],
                "promote": switched["promote"],
                "ticks": switched["ticks"],
            }
        )

        # Phase 4 — the carry: the promoted peer must keep the tune
        # the checkpoint carried — the parameter report and the
        # declared signal's reading holding the tuned value while
        # scans advance — and its served journal must order the
        # promotion's `role_changed` entries after the tune's
        # `command_settled`. A tune silently reverted to the declared
        # default fails here.
        want = control if tamper == "expect-original" else TUNED
        ticks = []
        carried_sample = None
        for _ in range(CARRY_TICKS):
            tracked, owner = pair.tick(duty_url, standby_url, failures)
            served = parameter_value(owner, component, name)
            if served != want:
                if tamper == "expect-original":
                    failures.append(
                        f"the promoted peer's parameter report reads "
                        f"{served} for {component}'s {name}, expected "
                        f"the original value {control}"
                    )
                else:
                    failures.append(
                        f"the promoted peer's parameter report reads "
                        f"{served} for {component}'s {name}, expected "
                        f"{TUNED} — the tune did not ride the "
                        "promotion's adopted state"
                    )
                raise Abort
            sample = simulate.snapshot_point(owner, point)
            if sample != want:
                if tamper == "expect-original":
                    failures.append(
                        f"the promoted peer's out point reads {sample}, "
                        f"expected the original value {control}"
                    )
                else:
                    failures.append(
                        f"the promoted peer's out point reads {sample}, "
                        f"expected {TUNED} on the declared signal's "
                        "source after promotion"
                    )
                raise Abort
            carried_sample = sample
            ticks.append(owner["tick"])
        journal = pair.get(f"{standby_url}/journal", "GET /journal", failures)
        tune_seqs = settled_seqs(journal, tune_command)
        promo_seqs = promotion_seqs(journal)
        if not tune_seqs:
            failures.append(
                "the promoted peer's served journal carries no "
                "command_settled for the tune — the settlement never "
                "reached its record"
            )
            raise Abort
        if not promo_seqs:
            failures.append(
                "the promoted peer's served journal carries no "
                "promotion role_changed — the switch never reached "
                "its record"
            )
            raise Abort
        if max(tune_seqs) >= min(promo_seqs):
            failures.append(
                f"the promoted peer's journal orders the tune's "
                f"command_settled at seq {max(tune_seqs)} at or after "
                f"the promotion's entries {promo_seqs} — the promotion "
                "must follow the settled tune"
            )
            raise Abort
        evidence["carried_through"] = ticks[-1]
        digest_entries.append(
            {
                "phase": "carry",
                "ticks": ticks,
                "value": parameter_value(owner, component, name),
                "sample": carried_sample,
                "tune_seqs": tune_seqs,
                "promotion_seqs": promo_seqs,
            }
        )

        # Phase 5 — the retune on the new active: a further
        # `set_parameter` through the same receipted path must settle
        # `applied` with a fresh receipt — never an adopted echo of
        # the pre-switch tune — the retuned value live on the
        # parameter report and the signal's reading, its journaled
        # settlement ordered after the promotion.
        retune = submit(standby_url, retune_command, failures)
        apply_tick = (
            retune.get("outcome", {}).get("accepted", {}).get("apply_tick")
        )
        _tracked, owner = pair.tick(duty_url, standby_url, failures)
        retuned_sample = assert_tuned(
            owner,
            component,
            name,
            RETUNED,
            point,
            "the promoted peer's",
            failures,
        )
        receipts = identical_receipts(duty_url, standby_url, failures)
        if not takeover.settled(receipts, retune_command):
            failures.append(
                "the retune never settled applied into the adopted "
                "receipt log"
            )
        journal = pair.get(
            f"{standby_url}/journal", "GET /journal", failures
        )
        retune_seqs = settled_seqs(journal, retune_command)
        if not retune_seqs or min(retune_seqs) <= max(promo_seqs):
            failures.append(
                f"the retune's settlement is journaled at {retune_seqs}, "
                f"not after the promotion's entries {promo_seqs} — no "
                "fresh receipt on the promoted peer"
            )
        if apply_tick is None or apply_tick <= switched["ticks"][-1]:
            failures.append(
                f"the retune's accepted receipt carries apply_tick "
                f"{apply_tick}, not after the promotion's handover "
                f"ticks — no fresh receipt on the promoted peer"
            )
        if failures:
            raise Abort
        evidence["retuned_at"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "retune",
                "receipt": retune,
                "value": parameter_value(owner, component, name),
                "sample": retuned_sample,
                "retune_seqs": retune_seqs,
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
        choices=["expect-original"],
        help="doctor the post-promotion expectation to the parameter's "
        "original value — the pass must fail naming the tune's "
        "carried reading",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = tune_pass(args, args.tamper)
    except Abort as abort:
        for line in abort.args:
            eprint(f"tune-carryover: {line}")
        return 1
    for failure in failures:
        eprint(f"tune-carryover: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"tune-carryover: the {args.tamper} case passed "
                "silently — the leg never noticed the tune still "
                "carried after promotion"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"tune-digest {digest} — tracking by tick "
        f"{evidence['converged']}, tuned at tick {evidence['tuned_at']}, "
        f"switched at tick {evidence['switched_at']}, carried through "
        f"tick {evidence['carried_through']}, retuned at tick "
        f"{evidence['retuned_at']}, roles restored at tick "
        f"{evidence['restored_at']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
