#!/usr/bin/env python3
"""The automatic-failover leg for the reference plant — the
consumer-side proof that the manifest-declared pair covers the
unattended half of the redundancy contract: a dead active's armed
standby self-promoting at its declared miss budget, claiming the
field writer, and continuing the run with no operator request
(WW-ENG-003, WW-LCM-001).

The pair leg (`ci/pair.py`) proves the receipted demote/promote
switch under the manifest's wiring; the `deploy` stage proves the
manifest's `failover_budget` declaration and the rig definition's
`--auto-promote` flag agree. This leg runs the armed pair — the rig
reads the standby wiring, the persistence fields, and the declared
budget out of `deploy/manifest.json` and spawns the released tooling
exactly as the pair legs do, the standby's `--auto-promote` carrying
the declared budget. The run:

- converges the declared standby to `tracking` through the pair
  leg's driven-tick loop, then stops the field-owning container —
  the honest consumer-side severance, the `docker stop` a dead
  controller is;
- drives the surviving peer's scans: each `POST /scan`'s checkpoint
  pull fails, `GET /role` reports the miss run (`standby` under the
  `degraded` sync state), and a foreign attachment's field probe
  stays fenced — the dead owner's silence is exactly the failure
  the writer claim exists to fence;
- asserts the self-promotion lands at the declared budget's scan
  boundary: the budget-th missed pull's `POST /scan` leaves `GET
  /role` reporting `active` at the expected tick;
- probes the plant's writer claim through the run's plant-protocol
  client: a foreign attachment's mutation stays `fenced` while the
  promoted peer's own writes demonstrably land — the claim the
  promotion took before its gate lifted now names this peer (where
  the release records the field owner's claim, the conditional
  `ensure_writer` probe under the dead owner's token answers
  `fenced` — the claim moved off its name; the earlier release
  line claims only on promotion, so the foreign probe reads the
  claim alone: open before, fenced after);
- proves the run continues bumplessly: subsequent driven scans keep
  writing the field — the served out-point's value landing — the
  field's level evolving under the promoted peer's steps, and a
  receipted kind-declared command settling `applied`;
- audits the durable record: the promoted peer's declared
  `--journal-file` carries `standby → promoting → active` in `seq`
  order attributed to the budget boundary — reading distinguishably
  from an operator-requested switch: where the recorded
  non-operator marker of the attributed role switch has landed the
  entry names it; where it has not, the entry carries no operator
  actor rather than a fabricated one.

A variant run severs the standby instead: the field owner's writes
run undisturbed and nothing reports a failover.

Usage:

    failover.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `failover-digest <sha256>` line prints — the check
runs two passes and compares them (`failover-nondeterministic`). A
contract violation reports `failover: …` lines on stderr and exits
1 — the check's `failover-failed`. `--tamper early-promotion`
doctors the leg's expectation — asserting the standby promoted
before the declared budget — so the leg proves its promotion gate
actually fires rather than passing an unexercised contract.
"""

import argparse
import hashlib
import json
import os
import re
import sys

import pair
import simulate


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The driven ticks each half of the pass runs: the post-promotion
# continuation proving the run kept going, and the standby-severed
# variant's undisturbed window.
CONTINUE_TICKS = 4
ACTOR = "ci-failover"

# The signal names resolving the leg's field points out of the
# emitted model — the served out-point the field must carry and the
# process input the promoted peer's steps move.
SIGNALS = {"cmd": "p101-cmd", "level": "level-primary"}

# The non-operator markers an attributed role switch names on the
# automatic path — the recorded self-promotion identities versus
# any operator actor. Where no marker has landed the role_changed
# entry stays unattributed and the leg asserts the absence.
NON_OPERATOR_MARKERS = {
    "failover",
    "auto_promote",
    "auto-promote",
    "automatic",
    "automatic_failover",
    "self_promote",
    "self-promote",
    "system",
}


def signal_points(model):
    """The `{key: point id}` map the emitted model's signal index
    declares for the leg's field points — None when the model
    declares no such points. A signal's `source` is the point it
    names; the lowest-signal-id-wins rule the served index applies."""
    by_name = {}
    for signal in model.get("signals", []):
        current = by_name.get(signal["name"])
        if current is None or signal["id"] < current[0]:
            by_name[signal["name"]] = (signal["id"], signal["source"])
    points = {
        key: by_name.get(name, (None, None))[1]
        for key, name in SIGNALS.items()
    }
    if any(point is None for point in points.values()):
        return None
    return points


def value(snapshot, point):
    """The point's latest sample value — `simulate.snapshot_point`."""
    return simulate.snapshot_point(snapshot, point)


def field_read(plant_io, point, failures):
    """One `read` through the plant protocol's unfenced surface —
    returns the served sample."""
    verdict = plant_io.request({"op": "read", "point": point})
    sample = verdict.get("sample") if isinstance(verdict, dict) else None
    if sample is None:
        failures.append(f"a field read of point {point} answered {verdict}")
        raise Abort
    return sample


def foreign_probe(plant_io, point, current):
    """A non-owner attachment's mutation probe — the same-value
    `write` the field's writer claim fences or admits: `fenced`
    while any claim stands, granted only while the field is
    unclaimed. The same value makes a granted probe idempotent."""
    return plant_io.request(
        {"op": "write", "point": point, "value": current}
    )


def named_probe(plant_io, owner_token):
    """The conditional writer claim under the recorded owner's token —
    granted while the field's claim still names it, `fenced` once a
    different owner stands."""
    return plant_io.request({"op": "ensure_writer", "owner": owner_token})


def probe_kind(verdict):
    """The claim-state word a probe verdict names — `granted`,
    `fenced`, `unclaimed` — with anything else kept raw for the
    failure line."""
    if verdict.get("result") in ("done", "claimed_shared"):
        return "granted"
    detail = json.dumps(verdict.get("error", {}))
    for kind in ("fenced", "unclaimed"):
        if kind in detail:
            return kind
    return f"unexpected {verdict}"


def expect_probe(failures, label, verdict, want):
    """Assert a probe verdict's claim-state word; returns it for the
    digest either way."""
    kind = probe_kind(verdict)
    if kind != want:
        failures.append(
            f"{label} answered {verdict} — expected the field {want} "
            "to this attachment"
        )
    return kind


def owner_token(preamble):
    """The field-ownership token a launched active's claim line
    reports — `field write-ownership claim held under owner token N`
    ahead of the listener where the release records it; None where
    the release line claims only on promotion and the field stands
    unclaimed until then."""
    for line in preamble:
        claimed = re.search(r"owner token (\d+)", line)
        if claimed:
            return int(claimed.group(1))
    return None


def attribution(entries, failures):
    """The operator-attribution audit on the journal's role_changed
    entries — the automatic promotion's record must read
    distinguishably from an operator-requested switch. Where a
    marker has landed it names the non-operator path; where none
    has, the entries carry no operator actor. Returns the digest's
    attribution word."""
    word = "unattributed"
    for entry in entries:
        change = entry.get("event", {}).get("role_changed")
        if change is None:
            continue
        marked = next(
            (
                key
                for key in (
                    "actor",
                    "attributed",
                    "attributed_to",
                    "initiated_by",
                    "source",
                    "cause",
                )
                if key in change
            ),
            None,
        )
        if marked is None:
            continue
        marker = change[marked]
        if marker not in NON_OPERATOR_MARKERS:
            failures.append(
                f"a role_changed entry carries {marked}={marker!r} — an "
                "operator actor on the self-promotion's record is a "
                "fabricated attribution; the automatic path records the "
                "non-operator marker or none"
            )
        else:
            word = f"{marked}={marker}"
    return word


def severed_owner_run(args, declared, points, budget, tamper, failures):
    """The failover half: converge the armed pair, sever the field
    owner, and assert the standby's self-promotion at the declared
    budget. Returns `(digest_entries, evidence)`."""
    digest_entries, evidence = [], {}
    rig = probe_io = None
    try:
        rig = pair.launch_pair(args, declared, auto_promote=budget)
        standby_url = rig.standby_url
        plant_io = rig.plant_io
        # The genuinely foreign attachment the fencing probes run on —
        # the run's own plant-protocol client can join a recorded
        # claim's holder set, so it never runs mutation probes.
        probe_io = simulate.PlantClient(rig.plant_addr)
        duty_token = owner_token(rig.duty_preamble)
        evidence["owner_claim"] = (
            "named" if duty_token is not None else "unclaimed"
        )

        # Phase 1 — convergence, then the baseline: the field carries
        # the owner's write, and the probes read the claim's standing
        # state.
        converged = rig.converge(failures)
        converge_tick = converged["ticks"][-1]
        owner = converged["owner"]
        evidence["converged"] = converge_tick
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )
        cmd = value(owner, points["cmd"])
        held = field_read(plant_io, points["cmd"], failures)["value"]
        if held != cmd:
            failures.append(
                f"the field carries {held} on the served out-point "
                f"while the field owner reports {cmd} — the converged "
                "pair's writes never landed"
            )
            raise Abort
        level_held = field_read(plant_io, points["level"], failures)[
            "value"
        ]
        probes = {"before": {"foreign": expect_probe(
            failures,
            "the pre-severance foreign probe",
            foreign_probe(probe_io, points["cmd"], cmd),
            "fenced" if duty_token is not None else "granted",
        )}}
        if duty_token is not None:
            probes["before"]["named"] = expect_probe(
                failures,
                "the pre-severance claim probe under the owner's token",
                named_probe(plant_io, duty_token),
                "granted",
            )

        # Phase 2 — the severance: the field-owning container stops,
        # and each driven scan on the surviving peer misses its pull.
        pair.stop(rig.duty)
        misses = []
        for miss in range(1, budget):
            pair.scan(standby_url, failures)
            report = pair.get(
                f"{standby_url}/role", "GET /role", failures
            )
            want = (
                "active" if tamper == "early-promotion" else "standby"
            )
            if report.get("role") != want:
                failures.append(
                    f"expected the standby {want} at miss {miss} under "
                    f"the declared budget {budget} — GET /role answers "
                    f"{report}"
                )
                raise Abort
            sync = report.get("sync")
            if not (isinstance(sync, dict) and "degraded" in sync):
                failures.append(
                    f"miss {miss} left the standby's sync {sync} — the "
                    "served role path reports no miss run"
                )
                raise Abort
            misses.append(
                {
                    "miss": miss,
                    "tick": report.get("tick"),
                    "role": "standby",
                    "sync": "degraded",
                }
            )
        # The orphaned field holds through the miss run — no writer,
        # no stepper — and the claim's fencing reads the dead owner's
        # standing silence.
        held = field_read(plant_io, points["cmd"], failures)["value"]
        orphaned_level = field_read(plant_io, points["level"], failures)[
            "value"
        ]
        if held != cmd:
            failures.append(
                f"the orphaned field moved to {held} during the miss "
                f"run — something kept writing past the owner's death"
            )
            raise Abort
        if orphaned_level != level_held:
            failures.append(
                f"the orphaned field's level moved to {orphaned_level} "
                "during the miss run — something kept stepping past "
                "the owner's death"
            )
            raise Abort
        probes["miss_run"] = {
            "foreign": expect_probe(
                failures,
                "the miss-run foreign probe",
                foreign_probe(probe_io, points["cmd"], cmd),
                "fenced" if duty_token is not None else "granted",
            )
        }
        if duty_token is not None:
            probes["miss_run"]["named"] = expect_probe(
                failures,
                "the miss-run claim probe under the dead owner's token",
                named_probe(plant_io, duty_token),
                "granted",
            )
        digest_entries.append(
            {"phase": "miss_run", "budget": budget, "misses": misses}
        )

        # Phase 3 — the budget-th miss's scan boundary: the pull fails
        # again, the count reaches the declared budget, and the
        # still-converged standby takes the field and lifts its gate
        # inside this same requested scan — the promotion and the
        # first field-owning scan land together.
        promoted = pair.scan(standby_url, failures)
        report = pair.get(f"{standby_url}/role", "GET /role", failures)
        if report.get("role") != "active":
            failures.append(
                "the standby never self-promoted at its declared "
                f"budget — GET /role answers {report}"
            )
            raise Abort
        promotion_tick = report.get("tick")
        if promotion_tick != converge_tick + budget:
            failures.append(
                f"the self-promotion landed at tick {promotion_tick}, "
                f"expected the budget-th miss's boundary "
                f"{converge_tick + budget}"
            )
            raise Abort
        evidence["promoted"] = promotion_tick
        # The claim now names the promoted peer: a foreign attachment
        # stays fenced while the promoted peer's own writes land —
        # the only writer the field accepts. Where the release records
        # the claim, the dead owner's conditional probe answers
        # fenced: the claim moved off its name.
        held = field_read(plant_io, points["cmd"], failures)["value"]
        if held != value(promoted, points["cmd"]):
            failures.append(
                f"the promoted peer's first field-owning scan left the "
                f"field at {held} while its image reports "
                f"{value(promoted, points['cmd'])} — its writes do not "
                "land, so the writer claim did not move to it"
            )
            raise Abort
        probes["promoted"] = {
            "foreign": expect_probe(
                failures,
                "the post-promotion foreign probe",
                foreign_probe(probe_io, points["cmd"], held),
                "fenced",
            )
        }
        if duty_token is not None:
            probes["promoted"]["named"] = expect_probe(
                failures,
                "the post-promotion claim probe under the dead owner's "
                "token",
                named_probe(plant_io, duty_token),
                "fenced",
            )
        digest_entries.append(
            {
                "phase": "promoted",
                "tick": promotion_tick,
                "role": report.get("role"),
            }
        )
        digest_entries.append({"phase": "claim", "probes": probes})

        # Phase 4 — the run continues bumplessly: each driven scan
        # keeps writing the field and stepping the process the dead
        # owner left frozen, and a receipted kind-declared command
        # settles applied on the promoted peer.
        continued = []
        for _ in range(CONTINUE_TICKS):
            snapshot = pair.scan(standby_url, failures)
            field = field_read(plant_io, points["cmd"], failures)["value"]
            level = field_read(plant_io, points["level"], failures)[
                "value"
            ]
            if field != value(snapshot, points["cmd"]):
                failures.append(
                    f"the promoted peer's tick {snapshot['tick']} left "
                    f"the field at {field} while its image reports "
                    f"{value(snapshot, points['cmd'])} — its writes do "
                    "not land"
                )
                raise Abort
            continued.append(
                {
                    "tick": snapshot["tick"],
                    "cmd": field,
                    "level": level,
                }
            )
        if continued[-1]["level"] == orphaned_level:
            failures.append(
                "the field's level never moved after the promotion — "
                "the promoted peer is not stepping the plant it claims"
            )
            raise Abort
        digest_entries.append(
            {"phase": "continued", "scans": continued}
        )

        schema = pair.get(
            f"{standby_url}/schema", "GET /schema", failures
        )
        declared_command = pair.declared_command(schema)
        if declared_command is None:
            failures.append(
                "the promoted peer's served registry declares no "
                "command — the receipted-continuation leg has nothing "
                "to exercise"
            )
            raise Abort
        component, spec = declared_command
        command = {
            "invoke": {
                "component": component,
                "command": spec["name"],
                "arguments": simulate.command_arguments(spec),
            }
        }
        status, receipt = pair.request(
            f"{standby_url}/command", {"command": command, "actor": ACTOR}
        )
        if status != 200 or simulate.receipt_outcome(receipt) != "accepted":
            failures.append(
                f"the promoted peer's /command answered {status} "
                f"{receipt}, expected an accepted receipt"
            )
            raise Abort
        settled = pair.scan(standby_url, failures)
        receipts = pair.get(
            f"{standby_url}/receipts", "GET /receipts", failures
        )
        if not any(
            entry.get("command") == command
            and simulate.receipt_outcome(entry) == "applied"
            for entry in receipts
        ):
            failures.append(
                f"the receipted {spec['name']} on {component} never "
                "settled applied on the promoted peer — the run's "
                "command path did not continue"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "command",
                "component": component,
                "command": spec["name"],
                "receipt": receipt,
            }
        )

        # Phase 5 — the durable record: the promoted peer's declared
        # journal file carries the self-promotion's transitions in
        # `seq` order attributed to the budget boundary — after the
        # miss run's ticks — with no operator actor recorded on the
        # automatic switch.
        journal_path = rig.standby_files.get("journal_file")
        if journal_path is None or not os.path.exists(journal_path):
            failures.append(
                "the promoted peer's declared journal file does not "
                "exist — the --journal-file flag was not honored"
            )
            raise Abort
        records = pair.journal_records(journal_path)
        boundaries = [
            record for kind, record in records if kind == "boundary"
        ]
        if boundaries != [{"run": 1, "tick": 0}]:
            failures.append(
                f"the promoted peer's journal boundaries are "
                f"{boundaries}, expected the single cold-start marker"
            )
        entries = [
            record for kind, record in records if kind == "entry"
        ]
        seqs = [entry["seq"] for entry in entries]
        if seqs != list(range(1, len(seqs) + 1)):
            failures.append(
                f"the promoted peer's journal seqs are not 1..n in "
                f"order: {seqs}"
            )
        transitions = pair.role_transitions(entries)
        want = [
            (promotion_tick - 1, "standby", "promoting"),
            (promotion_tick, "promoting", "active"),
        ]
        if transitions != want:
            failures.append(
                f"the promoted peer's journal carries the role "
                f"transitions {transitions}, expected {want} — the "
                "transition must record after the budget expiry"
            )
        evidence["attribution"] = attribution(entries, failures)
        served = pair.get(
            f"{standby_url}/journal", "GET /journal", failures
        )
        if pair.role_transitions(served) != transitions:
            failures.append(
                "the promoted peer's served journal diverges from its "
                "durable file — the monitor does not answer the record "
                "it persists"
            )
        digest_entries.append(
            {
                "phase": "record",
                "transitions": transitions,
                "attribution": evidence["attribution"],
                "journal_records": len(records),
            }
        )
        evidence["final_tick"] = settled["tick"]
        evidence["journal_records"] = len(records)
        if failures:
            raise Abort
    finally:
        if probe_io is not None:
            probe_io.close()
        if rig is not None:
            rig.close()
    return digest_entries, evidence


def severed_standby_run(args, declared, points, budget, failures):
    """The no-failover half: converge the armed pair, sever the
    standby, and assert the field owner's writes run undisturbed and
    nothing reports a failover. Returns `(digest_entries, evidence)`."""
    digest_entries, evidence = [], {}
    rig = probe_io = None
    try:
        rig = pair.launch_pair(args, declared, auto_promote=budget)
        plant_io = rig.plant_io
        probe_io = simulate.PlantClient(rig.plant_addr)
        duty_token = owner_token(rig.duty_preamble)

        converged = rig.converge(failures)
        pair.stop(rig.standby)
        undisturbed = []
        for _ in range(CONTINUE_TICKS):
            owner = pair.scan(rig.duty_url, failures)
            field = field_read(plant_io, points["cmd"], failures)["value"]
            if field != value(owner, points["cmd"]):
                failures.append(
                    f"the field owner's tick {owner['tick']} left the "
                    f"field at {field} while its image reports "
                    f"{value(owner, points['cmd'])} — the standby's "
                    "severance disturbed its writes"
                )
                raise Abort
            undisturbed.append({"tick": owner["tick"], "cmd": field})
        duty_role = pair.get(
            f"{rig.duty_url}/role", "GET /role", failures
        )
        if duty_role.get("role") != "active":
            failures.append(
                "the field owner's role moved after the standby's "
                f"severance — GET /role answers {duty_role}"
            )
            raise Abort
        transitions = rig.served_transitions(rig.duty_url, failures)
        if transitions:
            failures.append(
                f"the field owner journaled role transitions "
                f"{transitions} on its standby's death — a failover "
                "reported where none happened"
            )
            raise Abort
        probe = expect_probe(
            failures,
            "the standby-severed foreign probe",
            foreign_probe(
                probe_io, points["cmd"], undisturbed[-1]["cmd"]
            ),
            "fenced" if duty_token is not None else "granted",
        )
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )
        digest_entries.append(
            {
                "phase": "severed_standby",
                "scans": undisturbed,
                "duty_role": duty_role.get("role"),
                "foreign_probe": probe,
            }
        )
        evidence["undisturbed_tick"] = undisturbed[-1]["tick"]
    finally:
        if probe_io is not None:
            probe_io.close()
        if rig is not None:
            rig.close()
    return digest_entries, evidence


def failover_pass(args, tamper):
    """The failover run: the severed-owner self-promotion, then the
    severed-standby no-failover variant. Returns `(digest_entries,
    evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the failover leg "
            "has nothing to exercise"
        )
    _manifest, _duty_decl, standby_decl = declared
    budget = standby_decl.get("failover_budget")
    if (
        not isinstance(budget, int)
        or isinstance(budget, bool)
        or budget < 1
    ):
        raise Abort(
            "the manifest's standby declares no positive "
            "failover_budget — the failover leg has nothing to exercise"
        )
    with open(args.model) as handle:
        model = json.load(handle)
    points = signal_points(model)
    if points is None:
        raise Abort(
            "the emitted model declares no failover-leg field points "
            "— the leg has nothing to exercise"
        )
    digest_entries, evidence, failures = [], {}, []
    try:
        severed, severed_evidence = severed_owner_run(
            args, declared, points, budget, tamper, failures
        )
        digest_entries.extend(severed)
        evidence.update(severed_evidence)
        variant, variant_evidence = severed_standby_run(
            args, declared, points, budget, failures
        )
        digest_entries.extend(variant)
        evidence.update(variant_evidence)
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
        choices=["early-promotion"],
        help="doctor the leg's promotion expectation — the pass must "
        "fail naming the evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = failover_pass(
            args, args.tamper
        )
    except Abort as abort:
        for line in abort.args:
            eprint(f"failover: {line}")
        return 1
    for failure in failures:
        eprint(f"failover: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"failover: the {args.tamper} case passed silently — "
                "the leg never noticed the doctored expectation"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"failover-digest {digest} — tracking by tick "
        f"{evidence['converged']}, self-promoted at tick "
        f"{evidence['promoted']} on the declared miss budget with the "
        f"role record {evidence['attribution']}, run continued to "
        f"tick {evidence['final_tick']}, "
        f"{evidence['journal_records']} persisted journal records; "
        f"the severed standby left the owner undisturbed at tick "
        f"{evidence['undisturbed_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
