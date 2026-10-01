#!/usr/bin/env python3
"""The command-across-promotion leg for the reference plant — the
consumer-side proof that a customer's declared-command invocation
survives a switchover on the deployed redundant pair (WW-ENG-003,
WW-LCM-001, WW-FND-003 — decision 84's pair semantics).

The pair leg (`ci/legs/pair.py`) proves the manifest-declared pair runs and
switches bumplessly; the availability leg proves the served verdicts
agree with the receipted path. This leg proves the consumer-side half
decision 84 pins workspace-side (#381) and the settlement fix carries
(#529: a still-`Accepted` invocation rides the final-sync checkpoint
and settles exactly once on the new active): a customer invoking a
declared command while a switchover lands has evidence the receipt
survives — never lost, never double-applied.

The proving kind is the emitted model's `sequencer` — the exercise
program whose kind-declared `advance` command and kind-emitted
`step_completed` event the leg drives. All invocations go through the
released consumer tooling — `dcs-ctl invoke` (and `dcs-ctl write` for
the `run` hold) — never a raw `POST /command`. The run:

- converges the declared standby to `tracking` through the pair leg's
  driven-tick loop;
- holds the sequencer's `run` input (a model-declared writable point)
  across its declared step length so one `step_completed` emits —
  the pre-promotion emitted record;
- invokes `advance count=1` through `dcs-ctl invoke` on the field
  owner, asserting the `accepted` submission settles `applied` at the
  scan boundary with exactly one `command_settled` journal entry for
  that submission on the active, attributed to the leg's actor;
- issues the documented switch — `POST /demote` on the field owner,
  `POST /promote` on the converged standby — and drives the handover;
- on the promoted peer invokes the same declared command (`advance`
  with no `count`, the default-one shape — a distinct submission from
  the pre-switch one) through `dcs-ctl invoke`, asserting it settles
  `applied` with exactly one `command_settled` entry there, each
  settlement attributed to the peer that served it with no replay of
  the old peer's settlement (each command's filtered settlement count
  stays one on both peers after convergence);
- asserts the emitted-event records continue in tick order on the
  promoted peer — the pre-promotion `step_completed` entries intact
  as the prefix, new entries at greater ticks with unchanged
  component attribution and no duplicated (re-emitted) entries;
- submits a further `advance count=2` (a distinct third submission)
  on the current active immediately before the restore switch — the
  carried-`Accepted` shape — asserting it settles exactly once
  `applied` on the restored active with no applied settlement on the
  demoted peer (never lost, never double-applied);
- restores the pair's original roles — the manifest-declared duty
  controller `active` and its standby `tracking` again.

Usage:

    command_switch.py --ctl PATH --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `command-switch-digest <sha256>` line prints — the
check runs two passes and compares them
(`command-switch-nondeterministic`). A contract violation reports
`command-switch: …` lines on stderr and exits 1 — the check's
`command-switch-failed`. `--tamper zero-settlement` skips the
pre-switch submission while still asserting its settlement (the
zero-settlement count must fail); `--tamper double-settlement`
submits the pre-switch command twice while asserting exactly one
(the two-settlement count must fail).
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import pair
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored cases: a submission settling zero times and one
# settling twice must each surface the named diagnostic — never
# a silently miscounted exactly-once proof.
LEG = {
    "order": 180,
    "title": "the command-switch leg",
    "passes": "command-switch",
    "tools": {
        "ctl": "dcs-ctl",
    },
    "tampers": [
        {
            "name": "zero-settlement",
            "passed": "a zero-settlement case passed the command-switch leg",
            "missed": "the zero-settlement case did not report its named diagnostic",
            "evidence": ["expected exactly one settlement"],
        },
        {
            "name": "double-settlement",
            "passed": "a double-settlement case passed the command-switch leg",
            "missed": "the double-settlement case did not report its named diagnostic",
            "evidence": ["expected exactly one settlement"],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The driven ticks each phase runs. The actor the leg's receipted
# submissions declare through `dcs-ctl --actor`.
CONVERGE_TICKS = 4
EMIT_TICKS = 2
HANDOVER_TICKS = 4
RESTORE_TICKS = 4
ACTOR = "ci-command-switch"


def ctl_invoke(ctl, addr, component, command, arguments, failures):
    """One `dcs-ctl invoke` through the released tooling; returns the
    printed receipt after asserting the `accepted` submission under the
    leg's actor. `arguments` is the trailing `<name>=<value>` list."""
    env = dict(os.environ)
    env.pop("DCS_ACTOR", None)
    argv = [ctl, addr, "invoke", component, command, *arguments,
            "--actor", ACTOR]
    try:
        result = subprocess.run(argv, capture_output=True, text=True,
                                env=env, timeout=30)
    except Exception as error:
        failures.append(f"dcs-ctl invoke {component} {command} raised {error!r}")
        raise Abort
    if result.returncode != 0:
        failures.append(
            f"dcs-ctl invoke {component} {command} exited "
            f"{result.returncode}: {result.stderr.strip()}"
        )
        raise Abort
    try:
        receipt = json.loads(result.stdout)
    except json.JSONDecodeError:
        failures.append(
            f"dcs-ctl invoke {component} {command} printed no receipt"
        )
        raise Abort
    if simulate.receipt_outcome(receipt) != "accepted":
        failures.append(
            f"dcs-ctl invoke {component} {command} answered "
            f"{simulate.receipt_outcome(receipt)}, expected accepted"
        )
        raise Abort
    if receipt.get("actor") != ACTOR:
        failures.append(
            f"dcs-ctl invoke {component} {command} carries actor "
            f"{receipt.get('actor')!r}, expected {ACTOR!r}"
        )
        raise Abort
    return receipt


def ctl_write(ctl, addr, point, value, failures):
    """One `dcs-ctl write` holding a writable point; returns the
    printed receipt after asserting `accepted` under the leg's actor."""
    env = dict(os.environ)
    env.pop("DCS_ACTOR", None)
    argv = [ctl, addr, "write", str(point), value, "--actor", ACTOR]
    try:
        result = subprocess.run(argv, capture_output=True, text=True,
                                env=env, timeout=30)
    except Exception as error:
        failures.append(f"dcs-ctl write {point} raised {error!r}")
        raise Abort
    if result.returncode != 0:
        failures.append(
            f"dcs-ctl write {point} exited {result.returncode}: "
            f"{result.stderr.strip()}"
        )
        raise Abort
    try:
        receipt = json.loads(result.stdout)
    except json.JSONDecodeError:
        failures.append(f"dcs-ctl write {point} printed no receipt")
        raise Abort
    if simulate.receipt_outcome(receipt) != "accepted":
        failures.append(
            f"dcs-ctl write {point} answered "
            f"{simulate.receipt_outcome(receipt)}, expected accepted"
        )
        raise Abort
    return receipt


def invoke_command(component, command, arguments):
    """The receipted-path command object a `dcs-ctl invoke` submits —
    `arguments` the typed `{"name": {"kind": value}}` map."""
    return {
        "invoke": {
            "component": component,
            "command": command,
            "arguments": arguments,
        }
    }


def settlements_of(journal, command):
    """The `(seq, tick, outcome)` of a served journal's
    `command_settled` entries whose receipt answers `command`."""
    found = []
    for entry in journal:
        settled = entry.get("event", {}).get("command_settled")
        if settled is None:
            continue
        if settled.get("receipt", {}).get("command") == command:
            outcome = simulate.receipt_outcome(settled["receipt"])
            found.append((entry["seq"], entry["tick"], outcome))
    return found


def applied_settlements(journal, command):
    """The `applied` subset of `settlements_of`."""
    return [entry for entry in settlements_of(journal, command)
            if entry[2] == "applied"]


def assert_exactly_one(journal, command, label, failures):
    """Exactly one `command_settled` entry for `command` — and that one
    `applied` — or record the named failure."""
    settled = settlements_of(journal, command)
    if len(settled) != 1:
        failures.append(
            f"{label} carries {len(settled)} settlements, expected "
            f"exactly one settlement for the submission"
        )
        return None
    if settled[0][2] != "applied":
        failures.append(
            f"{label} settled {settled[0][2]}, expected the applied "
            "settlement the served verdict advertised"
        )
        return None
    return settled[0]


def emitted_step_completed(view, component):
    """The `(tick, step)` of a served `GET /resources` view's routed
    `event_emitted` records for `component`'s `step_completed` — the
    `History`-retained emission record the continuity assertions read.
    `step_completed` declares `history` retention (#478), so its
    consumer-visible record lives in the bounded event-history ring the
    resource view serves under the `history` mark — never the durable
    journal. The stream-local `seq` is excluded: each peer's store
    numbers its own records, so only tick and payload carry across the
    promoted peer join."""
    found = []
    for entry in view.get("components", []):
        if entry.get("name") != component:
            continue
        for record in entry.get("events", []):
            if record.get("retention") != "history":
                continue
            emitted = record.get("event", {}).get(
                "event_emitted", {}).get("event")
            if emitted is None:
                continue
            if emitted.get("component") != component:
                continue
            if emitted.get("event") != "step_completed":
                continue
            found.append((record["tick"],
                          emitted.get("fields", {}).get("step")))
    return found


def sequencer_target(model):
    """The leg's proving target out of the emitted model: the first
    `sequencer` component, returning `(component, run_point)` —
    `component` the served `<kind>:<id>` name, `run_point` the `run`
    port's bound writable point — or None."""
    bound = simulate.bound_points(model)
    writable = {point["id"] for point in model.get("io_points", [])
                if point.get("writable")}
    for entry in model.get("components", []):
        if entry.get("kind") != "sequencer":
            continue
        component = f"sequencer:{entry['id']}"
        run_point = bound.get((entry["id"], "run"))
        if run_point is None or run_point not in writable:
            continue
        return component, run_point
    return None


def journals(url, failures):
    """Both read-model records the assertions join: the served
    `GET /journal` and `GET /receipts`."""
    return (pair.get(f"{url}/journal", "GET /journal", failures),
            pair.get(f"{url}/receipts", "GET /receipts", failures))


def settle(rig, tracked_url, owner_url, failures, rounds=2):
    """Drive `rounds` tracking-first pair ticks after a receipted
    submission — the second absorbs the carried-command one-tick lag
    (issue #689) so the adopted settlement reads on both peers."""
    tracked = owner = None
    for _ in range(rounds):
        tracked, owner = rig.tick(tracked_url, owner_url, failures)
    return tracked, owner


def command_switch_pass(args, tamper):
    """The command-across-promotion run. Returns `(digest_entries,
    evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the command-switch "
            "leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    with open(args.model) as handle:
        model = json.load(handle)
    target = sequencer_target(model)
    if target is None:
        raise Abort(
            "the emitted model declares no sequencer with a writable "
            "run input — the command-switch leg has nothing to exercise"
        )
    component, run_point = target

    # Three distinct submissions of the same declared command — the
    # typed argument maps keep each submission's journaled audit
    # distinct so the exactly-once counts read per submission.
    pre_command = invoke_command(component, "advance",
                                 {"count": {"int": 1}})
    post_command = invoke_command(component, "advance", {})
    carried_command = invoke_command(component, "advance",
                                     {"count": {"int": 2}})
    pre_args = ["count=1"]
    post_args = []
    carried_args = ["count=2"]

    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared, None)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        duty_addr = duty_url.removeprefix("http://")

        # Phase 1 — convergence.
        converged = rig.converge(failures)
        evidence["converged"] = converged["ticks"][-1]
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )

        # Phase 2 — the pre-promotion emission: hold `run` across the
        # declared step length so one `step_completed` lands in the
        # served event-history record.
        ctl_write(args.ctl, duty_addr, run_point, "true", failures)
        for _ in range(EMIT_TICKS):
            rig.tick(standby_url, duty_url, failures)
        duty_view = pair.get(f"{duty_url}/resources",
                             "GET /resources", failures)
        pre_emitted = emitted_step_completed(duty_view, component)
        if not pre_emitted:
            failures.append(
                f"no emitted step_completed from {component} reached "
                "the active's served event record before the switch"
            )
            raise Abort
        evidence["emitted_before"] = pre_emitted[-1][0]
        digest_entries.append(
            {
                "phase": "emit-before",
                "emitted": pre_emitted,
            }
        )

        # Phase 3 — the pre-switch declared command through the
        # released tooling. Under `zero-settlement` the submission is
        # skipped while the settlement is still asserted (the count
        # reads zero); under `double-settlement` it is submitted twice
        # while exactly one is asserted (the count reads two).
        pre_receipt = None
        if tamper != "zero-settlement":
            pre_receipt = ctl_invoke(args.ctl, duty_addr, component,
                                     "advance", pre_args, failures)
            if tamper == "double-settlement":
                ctl_invoke(args.ctl, duty_addr, component,
                           "advance", pre_args, failures)
        _tracked, owner = settle(rig, standby_url, duty_url, failures)
        duty_journal, _receipts = journals(duty_url, failures)
        standby_journal, _receipts = journals(standby_url, failures)
        pre_settled = assert_exactly_one(
            duty_journal, pre_command,
            f"the pre-switch advance on {component}", failures)
        other = settlements_of(standby_journal, pre_command)
        if len(other) != 1:
            failures.append(
                f"the tracking standby carries {len(other)} settlements "
                "for the pre-switch advance, expected exactly one "
                "settlement adopted from the field owner"
            )
        if failures:
            raise Abort
        evidence["pre_settled_at"] = pre_settled[1]
        digest_entries.append(
            {
                "phase": "invoke-before",
                "component": component,
                "arguments": {"count": {"int": 1}},
                "receipt": pre_receipt,
                "settled": pre_settled,
            }
        )

        # Phase 4 — freeze the sequencer and switch forward.
        ctl_write(args.ctl, duty_addr, run_point, "false", failures)
        rig.tick(standby_url, duty_url, failures)
        switched = rig.switch(duty_url, standby_url, failures,
                              audit_receipts=True)
        evidence["switched_at"] = switched["demote"]["tick"]
        digest_entries.append(
            {
                "phase": "switch",
                "demote": switched["demote"],
                "promote": switched["promote"],
                "ticks": switched["ticks"],
            }
        )
        new_active, demoted = standby_url, duty_url
        new_addr = new_active.removeprefix("http://")

        # Phase 5 — the post-switch declared command on the promoted
        # peer: reopen with `reset` (the pre-switch advance completed
        # the two-step table), then the same declared command in its
        # default-one shape. Each settlement stays attributed to the
        # peer that served it — the pre-switch count stays one on both
        # peers, the post-switch count reads exactly one.
        reset_receipt = ctl_invoke(args.ctl, new_addr, component,
                                   "reset", [], failures)
        settle(rig, demoted, new_active, failures)
        post_receipt = ctl_invoke(args.ctl, new_addr, component,
                                  "advance", post_args, failures)
        _tracked, owner = settle(rig, demoted, new_active, failures)
        new_journal, _receipts = journals(new_active, failures)
        demoted_journal, _receipts = journals(demoted, failures)
        post_settled = assert_exactly_one(
            new_journal, post_command,
            f"the post-switch advance on {component}", failures)
        if len(settlements_of(demoted_journal, post_command)) != 1:
            failures.append(
                "the demoted peer carries no adopted settlement for "
                "the post-switch advance — the adopted audit is not "
                "one log"
            )
        for journal, name in ((new_journal, "promoted"),
                              (demoted_journal, "demoted")):
            if len(settlements_of(journal, pre_command)) != 1:
                failures.append(
                    f"the {name} peer carries "
                    f"{len(settlements_of(journal, pre_command))} "
                    "settlements for the pre-switch advance after "
                    "promotion, expected exactly one — the old peer's "
                    "settlement replayed"
                )
        if failures:
            raise Abort
        evidence["post_settled_at"] = post_settled[1]
        digest_entries.append(
            {
                "phase": "invoke-after",
                "component": component,
                "arguments": {},
                "reset_receipt": reset_receipt,
                "receipt": post_receipt,
                "settled": post_settled,
            }
        )

        # Phase 6 — emitted-event continuity on the promoted peer:
        # hold `run` so the reopened table emits again, then join the
        # pre-promotion prefix against the continued stream.
        ctl_write(args.ctl, new_addr, run_point, "true", failures)
        for _ in range(EMIT_TICKS):
            rig.tick(demoted, new_active, failures)
        new_view = pair.get(f"{new_active}/resources",
                            "GET /resources", failures)
        post_emitted = emitted_step_completed(new_view, component)
        if len(post_emitted) <= len(pre_emitted):
            failures.append(
                f"the promoted peer's emitted record holds "
                f"{len(post_emitted)} step_completed entries, not past "
                f"the pre-promotion {len(pre_emitted)} — the sequence "
                "did not continue"
            )
            raise Abort
        if post_emitted[:len(pre_emitted)] != pre_emitted:
            failures.append(
                "the promoted peer's emitted record does not continue "
                "the pre-promotion entries — attribution changed or a "
                "pre-promotion entry re-emitted"
            )
            raise Abort
        ticks = [tick for tick, _step in post_emitted]
        if any(later <= earlier for earlier, later in zip(ticks, ticks[1:])):
            failures.append(
                "the promoted peer's emitted events do not continue in "
                "tick order"
            )
            raise Abort
        if post_emitted[len(pre_emitted)][0] <= pre_emitted[-1][0]:
            failures.append(
                "the promoted peer's first post-promotion emission does "
                "not land after the pre-promotion record"
            )
            raise Abort
        evidence["emitted_through"] = post_emitted[-1][0]
        digest_entries.append(
            {
                "phase": "emit-after",
                "emitted": post_emitted,
            }
        )
        ctl_write(args.ctl, new_addr, run_point, "false", failures)
        rig.tick(demoted, new_active, failures)

        # Phase 7 — the carried invocation across the restore switch:
        # reopen on the current active, submit `advance count=2`
        # without settling, then restore the launch roles — the
        # promote boundary's final sync carries the still-`Accepted`
        # invocation and the restored active settles it exactly once.
        reset2 = ctl_invoke(args.ctl, new_addr, component,
                            "reset", [], failures)
        settle(rig, demoted, new_active, failures)
        carried_receipt = ctl_invoke(args.ctl, new_addr, component,
                                     "advance", carried_args, failures)
        if simulate.receipt_outcome(carried_receipt) != "accepted":
            failures.append(
                "the carried advance answered "
                f"{simulate.receipt_outcome(carried_receipt)}, expected "
                "the accepted submission the boundary carries"
            )
            raise Abort
        demote = rig.demote(new_active, failures, "the new field owner")
        promote = rig.promote(duty_url, failures, "the reconverged peer")
        # The final sync carries the still-`Accepted` invocation: the
        # restored peer must hold it carried before its first scan
        # settles it.
        promoted_receipts = pair.get(
            f"{duty_url}/receipts", "GET /receipts", failures)
        carried_held = [
            entry for entry in promoted_receipts
            if entry.get("command") == carried_command
        ]
        if len(carried_held) != 1 or simulate.receipt_outcome(
                carried_held[0]) != "accepted":
            failures.append(
                f"the promoted peer holds {carried_held} for the carried "
                "advance after the switch, expected the single carried "
                "accepted receipt — the invocation was lost at the "
                "boundary"
            )
            raise Abort
        carried_ticks = []
        tracked, owner = rig.tick(new_active, duty_url, failures)
        carried_ticks.append(owner["tick"])
        first_restored, _receipts = journals(duty_url, failures)
        first_settled = settlements_of(first_restored, carried_command)
        if len(first_settled) != 1 or first_settled[0][2] != "applied":
            failures.append(
                f"the carried advance carries {first_settled} after the "
                "first restored scan, expected exactly one applied "
                "settlement on the new active — the invocation was lost "
                "or double-applied across the boundary"
            )
            raise Abort
        for _ in range(HANDOVER_TICKS - 1):
            tracked, owner = rig.tick(new_active, duty_url, failures)
            carried_ticks.append(owner["tick"])
        restored_journal, _receipts = journals(duty_url, failures)
        old_journal, _receipts = journals(new_active, failures)
        carried_settled = settlements_of(restored_journal, carried_command)
        if len(carried_settled) != 1 or carried_settled[0][2] != "applied":
            failures.append(
                f"the carried advance carries {len(carried_settled)} "
                f"settlements ({carried_settled}), expected exactly one "
                "applied settlement on the new active — the invocation "
                "was lost or double-applied across the boundary"
            )
            raise Abort
        if len(settlements_of(old_journal, carried_command)) != 1:
            failures.append(
                "the demoted peer's adopted log carries no single "
                "settlement for the carried advance — the adopted audit "
                "is not one log"
            )
            raise Abort
        for _ in range(RESTORE_TICKS - HANDOVER_TICKS):
            tracked, owner = rig.tick(new_active, duty_url, failures)
            carried_ticks.append(owner["tick"])
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        standby_role = pair.get(f"{new_active}/role", "GET /role", failures)
        if duty_role.get("role") != "active":
            failures.append(
                f"the restored field owner reports "
                f"{duty_role.get('role')!r}, expected active — the pair "
                "was not left in its declared roles"
            )
        sync = standby_role.get("sync")
        if standby_role.get("role") != "standby" or not (
                isinstance(sync, dict) and "tracking" in sync):
            failures.append(
                "the restored standby never reconverged — GET /role "
                f"answers {standby_role}"
            )
        if failures:
            raise Abort
        evidence["carried_settled_at"] = carried_settled[0][1]
        evidence["restored_at"] = carried_ticks[-1]
        digest_entries.append(
            {
                "phase": "carry",
                "reset_receipt": reset2,
                "receipt": carried_receipt,
                "demote": demote,
                "promote": promote,
                "ticks": carried_ticks,
                "settled": carried_settled[0],
            }
        )
        digest_entries.append(
            {
                "phase": "restore",
                "ticks": carried_ticks,
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
    parser.add_argument("--ctl", required=True,
                        help="the released dcs-ctl binary")
    parser.add_argument("--plant-server", required=True)
    parser.add_argument("--controller", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--dynamics", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument(
        "--tamper",
        choices=["zero-settlement", "double-settlement"],
        help="doctor the pre-switch submission count — the pass must "
        "fail naming the exactly-once evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = command_switch_pass(
            args, args.tamper)
    except Abort as abort:
        for line in abort.args:
            eprint(f"command-switch: {line}")
        return 1
    for failure in failures:
        eprint(f"command-switch: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"command-switch: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored "
                "settlement count"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"command-switch-digest {digest} — tracking by tick "
        f"{evidence['converged']}, emitted at tick "
        f"{evidence['emitted_before']}, pre-switch advance settled at "
        f"tick {evidence['pre_settled_at']}, switched at tick "
        f"{evidence['switched_at']}, post-switch advance settled at "
        f"tick {evidence['post_settled_at']}, events through tick "
        f"{evidence['emitted_through']}, carried advance settled at "
        f"tick {evidence['carried_settled_at']}, roles restored at "
        f"tick {evidence['restored_at']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
