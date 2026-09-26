#!/usr/bin/env python3
"""The invoke argument-schema leg for the reference plant — the
consumer-side proof that the invoke-admission contract's argument
schema bound holds on the customer-owned redundant pair (WW-ENG-003,
WW-FND-004): the consumer-boundary mirror of the qa-rig leg at
qa_lane/scenarios/3450_invoke_undeclared_arg.py (#1006's leg for #981's
contract), every submission driven through the released `dcs-ctl` as
the consumer's command surface.

The pair leg (`ci/legs/pair.py`) proves the manifest-declared pair runs
and switches; the command-admission leg proves the bounded queue
refuses by name past its bound. This leg proves the schema half of the
same admission contract on the same declared deployment: a
`Command::Invoke`'s `arguments` are keyed by the declared
`CommandSpec.request` names — a submission carrying a name the schema
does not declare refuses by name at admission rather than settle
`applied` for a payload outside the declared request, while a declared
argument left absent stays legal, the kind owning the absent-argument
default. The proving target is the emitted model's `sequencer`, whose
`advance` declares `request [{name: count, kind: int}]` and whose
`step`/`done` outputs make a wrongly applied advance observable. The
run:

- converges the declared standby to `tracking` through the pair leg's
  driven-tick loop, the sequencer standing on step 1, then submits
  `advance bogus=1` — an argument name the declared request does not
  carry: the named `rejected{unknown_argument}` refusal answers, naming
  the component, the command, and the offending argument; the refusal
  is terminal on the wire, mirrored verbatim in the served receipt
  log, and journaled `command_settled` exactly once at the run's
  standing tick with no scan driven — nothing queued
  (`command_queue.depth` unmoved), nothing applied (the sequencer
  still reporting step 1) — and the tracking peer adopts the same
  record into its receipt log and journal;
- submits `advance` with `count` omitted — presence is never the
  schema's bound — answering `accepted` and settling `applied` at the
  scan boundary, the kind's absent-argument default moving the table
  exactly one step (`step` reads 2, `done` still low), the settlement
  journaled once and adopted onto the tracking peer;
- contrasts the ctl leg's third refusal class — `advance count=two`
  fails its declared-kind parse inside `dcs-ctl` before any
  submission: nonzero exit naming `invalid value`, no receipt printed,
  and no trace in the receipt log or journal — so the recorded
  evidence keeps the three argument-handling classes distinct:
  `rejected:unknown_argument` the receipted schema refusal, `applied`
  the kind-default resolution, and `invalid-value` the client-side
  parse refusal that leaves no plant-side record;
- leaves the pair in its launch roles: the field owner `active`, the
  standby `tracking`.

Usage:

    invoke_args.py --ctl PATH --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `invoke-args-digest <sha256>` line prints — the check
runs two passes and compares them (`invoke-args-nondeterministic`). A
contract violation reports `invoke-args: …` lines on stderr and exits
1 — the check's `invoke-args-failed`. A pinned release that predates
the invoke-admission contract — the undeclared name admitted rather
than refused, the served registry carrying no `advance` request
schema, or the released `dcs-ctl` refusing the probe before any
submission — reports `invoke-args-inconclusive` and exits 0: the
machinery the leg proves is absent from that release, not misbehaving.
`--tamper expect-queue-full` doctors the leg's own expectation —
asserting the undeclared name's refusal answers `queue_full` — so the
leg proves its named-refusal assertion fires rather than passing an
unexercised contract.
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
# The doctored case: a leg asserting the undeclared argument's refusal
# is `queue_full` must surface the named diagnostic on the honest
# `unknown_argument` answer — never a silently unexercised contract.
LEG = {
    "order": 280,
    "title": "the invoke argument-schema leg",
    "passes": "invoke-args",
    "tools": {
        "ctl": "dcs-ctl",
    },
    "tampers": [
        {
            "name": "expect-queue-full",
            "passed": "a expect-queue-full case passed the invoke-args leg",
            "missed": "the expect-queue-full case did not report its named diagnostic",
            "evidence": ["expected the named queue_full refusal"],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The actor every receipted submission declares through
# `dcs-ctl --actor`, and the tracking-first pair ticks an adoption or a
# settling boundary needs — comfortably past the adopted settlement's
# one-pull lag.
ACTOR = "ci-invoke-args"
SETTLE_TICKS = 3


def ctl(ctl_bin, addr, arguments):
    """One released `dcs-ctl` invocation; returns the CompletedProcess
    unasserted — the leg's probes answer both applied receipts and the
    named refusal classes. `DCS_ACTOR` is scrubbed so an ambient
    configured actor can never attribute an invocation the leg declares
    itself."""
    env = dict(os.environ)
    env.pop("DCS_ACTOR", None)
    return subprocess.run(
        [ctl_bin, addr, *[str(arg) for arg in arguments]],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def receipt_of(result):
    """The receipt a `dcs-ctl` answer printed — a rejected submission
    still prints its receipt — or None when the invocation answered
    no contract payload (a usage refusal never submits)."""
    try:
        decoded = json.loads(result.stdout)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(decoded, dict) or not isinstance(
        decoded.get("outcome"), dict
    ):
        return None
    return decoded


def outcome_key(receipt):
    """A receipt's normalized verdict — `accepted`, `applied`, or
    `rejected:<reason>` — the cross-class comparable the recorded
    evidence keeps distinct."""
    outcome = (receipt or {}).get("outcome")
    if not isinstance(outcome, dict) or not outcome:
        return "unknown"
    name = next(iter(outcome))
    if name == "rejected":
        body = outcome.get("rejected")
        reason = body.get("reason") if isinstance(body, dict) else {}
        return "rejected:" + (
            next(iter(reason))
            if isinstance(reason, dict) and reason
            else "?"
        )
    return name


def invoke_command(component, command, arguments):
    """The receipted-path command a `dcs-ctl invoke` submits —
    `arguments` the typed `{"name": {"kind": value}}` map."""
    return {
        "invoke": {
            "component": component,
            "command": command,
            "arguments": arguments,
        }
    }


def settlements(journal, command):
    """The `(seq, tick, receipt)` of a served journal's
    `command_settled` entries for one submission — matched on the
    command and the leg's actor."""
    found = []
    for entry in journal:
        settled = entry.get("event", {}).get("command_settled")
        if settled is None:
            continue
        receipt = settled.get("receipt", {})
        if (
            receipt.get("command") == command
            and receipt.get("actor") == ACTOR
        ):
            found.append((entry.get("seq"), entry.get("tick"), receipt))
    return found


def sequencer_target(model):
    """The leg's proving target out of the emitted model: the first
    `sequencer` component, returning `(component, step_point,
    done_point)` — `component` the served `<kind>:<id>` name, the
    points the kind's `step`/`done` status outputs bind — or None."""
    bound = simulate.bound_points(model)
    for entry in sorted(
        model.get("components", []), key=lambda item: item["id"]
    ):
        if entry.get("kind") != "sequencer":
            continue
        step = bound.get((entry["id"], "step"))
        done = bound.get((entry["id"], "done"))
        if step is None or done is None:
            continue
        return f"sequencer:{entry['id']}", step, done
    return None


def advance_spec(schema, component):
    """The served registry's declared `advance` CommandSpec for
    `component` — the request schema the argument bound reads — or
    None when the served surface declares no such command."""
    for entry in schema.get("interfaces", []):
        if entry.get("name") != component:
            continue
        for spec in entry.get("interface", {}).get("commands", []):
            if (
                spec.get("name") == "advance"
                and spec.get("adapted") == "declared"
            ):
                return spec
    return None


def tracking(role):
    """Whether a `GET /role` report is the settled tracking standby."""
    sync = role.get("sync") if isinstance(role, dict) else None
    return (
        role.get("role") == "standby"
        and isinstance(sync, dict)
        and "tracking" in sync
    )


def invoke_args_pass(args, tamper):
    """The argument-schema run: converge, undeclared-name refusal,
    omitted-name apply, malformed-value contrast. Returns
    `(digest_entries, evidence, failures, inconclusive)` —
    `inconclusive` the report line a release predating the contract
    answers, or None."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the invoke-args "
            "leg has nothing to exercise"
        )
    with open(args.model) as handle:
        model = json.load(handle)
    target = sequencer_target(model)
    if target is None:
        raise Abort(
            "the emitted model declares no sequencer with bound "
            "step/done outputs — the invoke-args leg has nothing to "
            "exercise"
        )
    component, step_point, done_point = target
    digest_entries, evidence, failures = [], {}, []
    inconclusive = None
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        duty_addr = duty_url.removeprefix("http://")

        # Phase 1 — convergence, the quiet-table baseline the
        # nothing-applied observation reads, and the served request
        # schema the probes are built from.
        converged = rig.converge(failures)
        owner = converged["owner"]
        pre_tick = owner["tick"]
        queue = owner.get("command_queue") or {}
        depth0 = queue.get("depth")
        attempts0 = queue.get("attempts")
        step0 = simulate.snapshot_point(owner, step_point)
        done0 = simulate.snapshot_point(owner, done_point)
        if step0 != {"int": 1} or done0 != {"bool": False}:
            failures.append(
                f"the converged sequencer does not stand quiet on "
                f"step 1 — step reads {step0}, done {done0} — the "
                "leg's observation baseline is absent"
            )
            raise Abort
        evidence["converged"] = pre_tick
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
                "step": step0,
                "queue": {"depth": depth0, "attempts": attempts0},
            }
        )
        schema = pair.get(f"{duty_url}/schema", "GET /schema", failures)
        spec = advance_spec(schema, component)
        if spec is None:
            inconclusive = (
                "the served registry declares no `advance` request "
                "schema — the pinned release predates the "
                "invoke-admission contract"
            )
            return digest_entries, evidence, failures, inconclusive

        # Phase 2 — the undeclared-argument invoke: `advance bogus=1`
        # names an argument the declared request does not carry. The
        # named `unknown_argument` refusal is the contract — an
        # admitted submission means the bound is absent from this
        # release. The `expect-queue-full` tamper doctors the leg's
        # own expectation: asserting the refusal answers `queue_full`
        # must fail on the honest `unknown_argument` answer — and on
        # an admitted answer alike, the doctored name never matching.
        declared_names = {
            argument.get("name")
            for argument in spec.get("request") or []
        }
        undeclared = "bogus"
        while undeclared in declared_names:
            undeclared += "_x"
        refused_command = invoke_command(
            component, "advance", {undeclared: {"int": 1}}
        )
        result = ctl(
            args.ctl,
            duty_addr,
            [
                "invoke",
                component,
                "advance",
                f"{undeclared}=1",
                "--actor",
                ACTOR,
            ],
        )
        receipt = receipt_of(result)
        key = outcome_key(receipt)
        if tamper is None:
            if receipt is None:
                if result.returncode == 0:
                    failures.append(
                        "the undeclared-argument invoke exited zero "
                        "printing no receipt — the answer is not the "
                        "contract's"
                    )
                    raise Abort
                inconclusive = (
                    "the released dcs-ctl refused the undeclared-name "
                    "probe before any submission — the server-side "
                    "admission bound is unexercised"
                )
                return digest_entries, evidence, failures, inconclusive
            if key == "accepted":
                inconclusive = (
                    "the undeclared-argument invoke was admitted "
                    "rather than refused — the pinned release predates "
                    "the invoke-admission contract"
                )
                return digest_entries, evidence, failures, inconclusive
            if key == "rejected:queue_full":
                inconclusive = (
                    "the undeclared-argument invoke met the bounded "
                    "queue's admission before the schema check could "
                    "answer — the refusal half is uncovered"
                )
                return digest_entries, evidence, failures, inconclusive
            if key == "rejected:not_active":
                inconclusive = (
                    "the serving peer left the active role mid-leg — "
                    "the refusal half is uncovered"
                )
                return digest_entries, evidence, failures, inconclusive
        want = (
            "queue_full" if tamper == "expect-queue-full"
            else "unknown_argument"
        )
        if key != f"rejected:{want}":
            failures.append(
                f"the undeclared-argument invoke answered {key}, "
                f"expected the named {want} refusal"
            )
            raise Abort
        if receipt.get("command") != refused_command:
            failures.append(
                f"the refusal's echoed command reads "
                f"{receipt.get('command')}, expected {refused_command}"
            )
        if receipt.get("actor") != ACTOR:
            failures.append(
                f"the refusal carries actor {receipt.get('actor')!r}, "
                f"expected {ACTOR!r}"
            )
        if "unknown_argument" not in result.stderr:
            failures.append(
                f"the rejected invoke did not name unknown_argument "
                f"on stderr: {result.stderr.strip()!r}"
            )
        if tamper is None:
            named = (
                receipt["outcome"]["rejected"].get("reason") or {}
            ).get("unknown_argument") or {}
            if (
                named.get("component") != component
                or named.get("command") != "advance"
                or named.get("argument") != undeclared
            ):
                failures.append(
                    f"the unknown_argument refusal names "
                    f"{json.dumps(named, sort_keys=True)} — the "
                    f"submission's offending name is {undeclared} on "
                    f"{component}'s advance"
                )
        if failures:
            raise Abort
        evidence["refused_at"] = pre_tick
        digest_entries.append(
            {
                "phase": "refused",
                "command": refused_command,
                "receipt": receipt,
            }
        )

        # Phase 3 — the refusal's served evidence under the
        # admitted-refusal convention: the receipt log mirrors the
        # terminal verdict verbatim, the journal carries exactly one
        # `command_settled` at the run's standing tick — no scan
        # driven — the queue never took the submission, and the
        # sequencer's outputs stand unmoved: nothing applied.
        receipts = pair.get(
            f"{duty_url}/receipts", "GET /receipts", failures
        )
        served = [
            entry
            for entry in receipts
            if entry.get("command") == refused_command
        ]
        if len(served) != 1 or outcome_key(served[0]) != (
            "rejected:unknown_argument"
        ):
            failures.append(
                f"the served receipt log carries {served} for the "
                "refused invoke — one terminal rejected "
                "unknown_argument entry expected"
            )
            raise Abort
        snapshot = pair.get(
            f"{duty_url}/snapshot", "GET /snapshot", failures
        )
        queue1 = snapshot.get("command_queue") or {}
        if queue1.get("depth") != depth0:
            failures.append(
                f"the refused submission entered the pending queue — "
                f"command_queue.depth {depth0} -> "
                f"{queue1.get('depth')}"
            )
        if (
            simulate.snapshot_point(snapshot, step_point) != step0
            or simulate.snapshot_point(snapshot, done_point) != done0
        ):
            failures.append(
                "the sequencer's outputs moved under the refused "
                "invoke — the undeclared-argument payload applied"
            )
        journal = pair.get(f"{duty_url}/journal", "GET /journal", failures)
        settles = settlements(journal, refused_command)
        if len(settles) != 1:
            failures.append(
                f"the served journal carries {len(settles)} "
                "command_settled records for the refused invoke, "
                "expected exactly one"
            )
        else:
            seq, tick, journaled = settles[0]
            if outcome_key(journaled) != "rejected:unknown_argument":
                failures.append(
                    f"the journaled settle carries "
                    f"{outcome_key(journaled)} for the refused invoke, "
                    "expected rejected:unknown_argument"
                )
            if tick != pre_tick:
                failures.append(
                    f"the refusal journaled at tick {tick}, expected "
                    f"the submission's standing tick {pre_tick} — the "
                    "admission refusal is terminal before any scan"
                )
        if failures:
            raise Abort
        # The adoption: tracking-first pair ticks carry the refused
        # record onto the standby — one adopted receipt log, one
        # journaled settle, the quiet table intact.
        _tracked, owner = rig.tick(standby_url, duty_url, failures)
        standby_receipts = pair.get(
            f"{standby_url}/receipts", "GET /receipts", failures
        )
        receipts = pair.get(
            f"{duty_url}/receipts", "GET /receipts", failures
        )
        if standby_receipts != receipts:
            failures.append(
                "the peers' receipt logs diverged across the "
                "refusal's adoption — the adopted audit is not one log"
            )
        standby_journal = pair.get(
            f"{standby_url}/journal", "GET /journal", failures
        )
        adopted = settlements(standby_journal, refused_command)
        if len(adopted) != 1 or outcome_key(adopted[0][2]) != (
            "rejected:unknown_argument"
        ):
            failures.append(
                f"the tracking peer's journal carries {adopted} for "
                "the refused invoke — one adopted rejected "
                "unknown_argument settle expected"
            )
        if (
            simulate.snapshot_point(owner, step_point) != step0
            or simulate.snapshot_point(owner, done_point) != done0
        ):
            failures.append(
                "the sequencer left step 1 under the refused invoke "
                "— the undeclared-argument payload applied"
            )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "refusal-evidence",
                "served": served[0],
                "journaled": {
                    "seq": settles[0][0],
                    "tick": settles[0][1],
                    "receipt": settles[0][2],
                },
                "queue": {
                    "depth": queue1.get("depth"),
                    "attempts": queue1.get("attempts"),
                },
                "adopted": {
                    "seq": adopted[0][0],
                    "tick": adopted[0][1],
                },
            }
        )

        # Phase 4 — the omitted-argument invoke: `advance` with `count`
        # withheld resolves the kind's absent-argument default — the
        # schema bounds names and kinds, never presence — and applies
        # at the scan boundary, the table moving exactly one step.
        applied_command = invoke_command(component, "advance", {})
        result2 = ctl(
            args.ctl,
            duty_addr,
            ["invoke", component, "advance", "--actor", ACTOR],
        )
        receipt2 = receipt_of(result2)
        if result2.returncode != 0 or outcome_key(receipt2) != (
            "accepted"
        ):
            failures.append(
                f"the omitted-argument invoke exited "
                f"{result2.returncode} answering "
                f"{outcome_key(receipt2)}, expected an accepted "
                "receipt — the schema bounds names, not presence"
            )
            raise Abort
        if receipt2.get("command") != applied_command or (
            receipt2.get("actor") != ACTOR
        ):
            failures.append(
                f"the omitted-argument invoke echoed {receipt2}, "
                f"expected {applied_command} under actor {ACTOR}"
            )
            raise Abort
        _tracked, owner = rig.tick(standby_url, duty_url, failures)
        receipts = pair.get(
            f"{duty_url}/receipts", "GET /receipts", failures
        )
        settled = [
            entry
            for entry in receipts
            if entry.get("command") == applied_command
            and outcome_key(entry) == "applied"
        ]
        if len(settled) != 1:
            failures.append(
                f"the omitted-argument invoke settled {settled}, "
                "expected exactly one applied receipt"
            )
            raise Abort
        journal = pair.get(f"{duty_url}/journal", "GET /journal", failures)
        settles2 = settlements(journal, applied_command)
        if len(settles2) != 1 or outcome_key(settles2[0][2]) != (
            "applied"
        ):
            failures.append(
                f"the served journal carries {settles2} for the "
                "omitted-argument invoke — exactly one applied "
                "command_settled expected"
            )
            raise Abort
        step1 = simulate.snapshot_point(owner, step_point)
        done1 = simulate.snapshot_point(owner, done_point)
        if step1 != {"int": 2} or done1 != {"bool": False}:
            failures.append(
                f"the omitted-argument advance left step {step1}, "
                f"done {done1} — the kind's absent-argument default "
                "moves the table exactly one step"
            )
        standby_receipts = pair.get(
            f"{standby_url}/receipts", "GET /receipts", failures
        )
        if standby_receipts != receipts:
            failures.append(
                "the peers' receipt logs diverged across the "
                "omitted-argument settle — the adopted audit is not "
                "one log"
            )
        standby_journal = pair.get(
            f"{standby_url}/journal", "GET /journal", failures
        )
        adopted2 = settlements(standby_journal, applied_command)
        if len(adopted2) != 1 or outcome_key(adopted2[0][2]) != (
            "applied"
        ):
            failures.append(
                f"the tracking peer's journal carries {adopted2} for "
                "the omitted-argument invoke — one adopted applied "
                "settle expected"
            )
        if failures:
            raise Abort
        evidence["applied_at"] = settles2[0][1]
        digest_entries.append(
            {
                "phase": "omitted",
                "command": applied_command,
                "receipt": receipt2,
                "settled": settled[0],
                "journaled": {
                    "seq": settles2[0][0],
                    "tick": settles2[0][1],
                    "receipt": settles2[0][2],
                },
                "step": step1,
                "done": done1,
            }
        )

        # Phase 5 — the malformed-value contrast: `advance count=two`
        # fails its declared-kind parse inside `dcs-ctl` before any
        # submission — nonzero exit naming `invalid value`, no receipt,
        # and no plant-side record. The three classes stay named
        # distinctly: the receipted `unknown_argument` schema refusal,
        # the kind-default `applied`, and the client-side
        # `invalid-value` parse refusal nothing submitted.
        result3 = ctl(
            args.ctl,
            duty_addr,
            [
                "invoke",
                component,
                "advance",
                "count=two",
                "--actor",
                ACTOR,
            ],
        )
        if result3.returncode == 0:
            failures.append(
                "dcs-ctl invoke with a malformed argument exited zero"
            )
        elif "invalid value" not in result3.stderr:
            failures.append(
                f"the malformed argument's failure names "
                f"{result3.stderr.strip()!r}, not invalid value"
            )
        elif receipt_of(result3) is not None:
            failures.append(
                "the malformed-value invoke printed a receipt — the "
                "parse refusal submitted where nothing may submit"
            )
        mine = [
            entry
            for entry in pair.get(
                f"{duty_url}/receipts", "GET /receipts", failures
            )
            if entry.get("actor") == ACTOR
        ]
        if [
            (entry.get("command"), outcome_key(entry))
            for entry in mine
        ] != [
            (refused_command, "rejected:unknown_argument"),
            (applied_command, "applied"),
        ]:
            failures.append(
                f"the receipt log's leg submissions read {mine} — "
                "the refused invoke, the applied invoke, and nothing "
                "for the parse-refused one expected"
            )
        journal = pair.get(f"{duty_url}/journal", "GET /journal", failures)
        journaled = [
            entry
            for entry in journal
            if "command_settled" in entry.get("event", {})
            and entry["event"]["command_settled"]
            .get("receipt", {})
            .get("actor")
            == ACTOR
        ]
        if len(journaled) != 2:
            failures.append(
                f"the journal carries {len(journaled)} command_settled "
                "records for the leg's submissions — the refusal and "
                "the apply, never the parse-refused one"
            )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "parse-refusal",
                "classes": {
                    "undeclared_name": "rejected:unknown_argument",
                    "omitted_declared": "applied",
                    "malformed_value": "invalid-value",
                },
                "receipts": mine,
                "journaled": len(journaled),
            }
        )

        # Phase 6 — the pair left in its launch roles.
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        standby_role = pair.get(
            f"{standby_url}/role", "GET /role", failures
        )
        if duty_role.get("role") != "active":
            failures.append(
                f"the field owner reports {duty_role.get('role')!r} "
                "after the leg, expected active"
            )
        if not tracking(standby_role):
            failures.append(
                f"the tracking peer reports {standby_role} after the "
                "leg, expected standby/tracking"
            )
        if failures:
            raise Abort
        evidence["final_tick"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "roles",
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
    return digest_entries, evidence, failures, inconclusive


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--ctl", required=True, help="the released dcs-ctl binary"
    )
    parser.add_argument("--plant-server", required=True)
    parser.add_argument("--controller", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--dynamics", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument(
        "--tamper",
        choices=["expect-queue-full"],
        help="doctor the leg's own expectation — the pass must fail "
        "naming the honest unknown_argument refusal",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures, inconclusive = (
            invoke_args_pass(args, args.tamper)
        )
    except Abort as abort:
        for line in abort.args:
            eprint(f"invoke-args: {line}")
        return 1
    for failure in failures:
        eprint(f"invoke-args: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"invoke-args: the {args.tamper} case passed silently "
                "— the leg never noticed the honest refusal"
            )
        return 1
    if failures:
        return 1
    if inconclusive is not None:
        print(f"invoke-args-inconclusive — {inconclusive}")
        return 0
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"invoke-args-digest {digest} — the undeclared argument "
        f"refused unknown_argument at tick {evidence['refused_at']}, "
        f"the omitted argument applied at tick "
        f"{evidence['applied_at']}, the malformed value refused "
        "invalid-value before submission"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
