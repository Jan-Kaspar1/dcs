#!/usr/bin/env python3
"""The automatic-failover leg for the reference plant — the
consumer-side proof that the manifest-declared pair covers the
unattended half of the redundancy contract: a dead active's armed
standby self-promoting at its declared miss budget, claiming the
field writer, and continuing the run with no operator request
(WW-ENG-003, WW-LCM-001).

The pair leg (`ci/legs/pair.py`) proves the receipted demote/promote
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

A measurement run then exercises decision 42's declared measurement
contract on the settled, tracking pair — the emitted model's
failover-select reading `primary`/`backup` field points, `out`
feeding the threshold chain, `backup_active` feeding the declared
managed Bool alarm through its carrier point, and the chain's
`on_bad_demand` fallback — the seam resolved from the artifact's
wiring, never hard-coded ids. Through the plant protocol's
quality-fault surface the run:

- degrades the primary level source and asserts through the
  active's monitor that the backup-serving flag asserts, the chain
  keeps evaluating the selected backup measurement per the declared
  setpoint table, and the managed `backup-active` alarm annunciates
  with journaled `point_changed` evidence;
- degrades the backup as well and asserts the declared
  all-sources-bad fallback — the selection untrusted, `demand` at
  `on_bad_demand` with the alarmed state standing — rather than
  control on bad data;
- restores the backup then the primary, asserting the selection
  returns to the primary and the alarm returns per its declared
  lifecycle — the unacknowledged latch standing until the receipted
  ack clears it — and audits the durable journal's transition
  order, the pair's roles unchanged.

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
actually fires rather than passing an unexercised contract;
`controls-on-bad` expects the station still controlling on the bad
primary and `nonzero-fallback` expects the fallback demand nonzero,
so a release that stopped failing over or held a nonzero fallback
demand would still fail the leg.
"""

import argparse
import hashlib
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import managed_lifecycle
import pair
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored cases: legs expecting the standby promoted before
# the declared budget, the station still controlling on the bad
# primary, or a nonzero fallback demand must surface the named
# diagnostic — never a silently unexercised contract.
LEG = {
    "order": 140,
    "title": "the failover leg",
    "passes": "failover-leg",
    "tampers": [
        {
            "name": "early-promotion",
            "passed": "a doctored early-promotion expectation passed the failover leg",
            "missed": "the early-promotion case did not report its named diagnostic",
            "evidence": ["expected the standby active at miss"],
        },
        {
            "name": "controls-on-bad",
            "passed": "a doctored controls-on-bad expectation passed the failover leg",
            "missed": "the controls-on-bad case did not report its named diagnostic",
            "evidence": ["expected the station still controlling on the bad primary"],
        },
        {
            "name": "nonzero-fallback",
            "passed": "a doctored nonzero-fallback expectation passed the failover leg",
            "missed": "the nonzero-fallback case did not report its named diagnostic",
            "evidence": ["expected the fallback demand nonzero"],
        },
    ],
}

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


# --------------------------------------------------------------------
# The measurement-failover run: the emitted model's declared
# failover-select / threshold-chain / managed-alarm seam, walked
# through the plant protocol's quality-fault surface on the settled,
# tracking pair.

# The driven scan bound each landing waits out — the carried-point
# hops the alarm's carrier adds cost a few scans.
MEASUREMENT_BOUND = 16

# The injected non-Good the fault surface carries — the same quality
# the lifecycle and burst legs drive.
BAD_QUALITY = {"bad": "device_fault"}


def sample(snapshot, point):
    """The point's latest served sample — `{"value", "quality",
    "tick"}` or None."""
    for entry in snapshot.get("points", []):
        if entry.get("point") == point:
            return entry.get("sample")
    return None


def quality_key(sample):
    """The served quality as a comparable key — `good`,
    `uncertain:<reason>`, `bad:<reason>`."""
    quality = (sample or {}).get("quality")
    if quality == "good":
        return "good"
    if isinstance(quality, dict) and len(quality) == 1:
        kind, reason = next(iter(quality.items()))
        return f"{kind}:{reason}"
    return f"unexpected {quality}"


def demand_stage(snapshot, point):
    """The served demand's integer stage, or None."""
    entry = sample(snapshot, point)
    return (entry or {}).get("value", {}).get("int")


def measurement_wiring(model):
    """The emitted model's declared measurement-failover seam,
    resolved from the artifact's wiring so the exercise lands on
    declared points, never hard-coded ids: the failover-select's
    bound `primary`/`backup` sources, `out`, and `backup_active`;
    the threshold chain's bound `level`/`demand` with its declared
    setpoint table and `on_bad_demand`; and the managed Bool alarm
    consuming the backup-serving flag's declared carrier — its bound
    `alarm`, `unacknowledged`, and writable `ack` points. None when
    the model declares no such seam."""
    bound_in = {}
    bound_out = {}
    mirrored = {}
    for connection in model.get("connections", []):
        frm, to = connection.get("from", {}), connection.get("to", {})
        if "point" in frm and "port" in to:
            bound_in[
                (to["port"]["component"], to["port"]["name"])
            ] = frm["point"]
        elif "port" in frm and "point" in to:
            bound_out[
                (frm["port"]["component"], frm["port"]["name"])
            ] = to["point"]
        elif "point" in frm and "point" in to:
            mirrored[frm["point"]] = to["point"]
    select = next(
        (
            component
            for component in model.get("components", [])
            if component.get("kind") == "failover-select"
        ),
        None,
    )
    chain = next(
        (
            component
            for component in model.get("components", [])
            if component.get("kind") == "threshold-chain"
        ),
        None,
    )
    if select is None or chain is None:
        return None
    sel, chn = select["id"], chain["id"]
    points = {
        "primary": bound_in.get((sel, "primary")),
        "backup": bound_in.get((sel, "backup")),
        "out": bound_out.get((sel, "out")),
        "backup_active": bound_out.get((sel, "backup_active")),
        "level": bound_in.get((chn, "level")),
        "demand": bound_out.get((chn, "demand")),
    }
    if any(point is None for point in points.values()):
        return None
    # The selected level must be the chain's delivered one.
    if mirrored.get(points["level"]) != points["out"]:
        return None
    parameters = chain.get("parameters", {})
    table = {}
    for key in ("cutoff", "stop", "start", "lag_start", "high"):
        entry = parameters.get(key, {})
        if "float" not in entry:
            return None
        table[key] = entry["float"]
    on_bad = parameters.get("on_bad_demand", {}).get("int")
    if on_bad is None:
        return None
    # The alarm the flag feeds: the carrier In point reading
    # `backup_active`, bound to a managed Bool alarm's `in`.
    carrier = next(
        (
            point
            for point, source in mirrored.items()
            if source == points["backup_active"]
        ),
        None,
    )
    alarmed = next(
        (
            component
            for component in model.get("components", [])
            if component.get("kind") == "managed-bool-latching-alarm"
            and bound_in.get((component["id"], "in")) == carrier
        ),
        None,
    )
    if carrier is None or alarmed is None:
        return None
    alm = alarmed["id"]
    points["carrier"] = carrier
    points["alarm"] = bound_out.get((alm, "alarm"))
    points["unack"] = bound_out.get((alm, "unacknowledged"))
    points["ack"] = bound_in.get((alm, "ack"))
    if any(
        points[key] is None for key in ("alarm", "unack", "ack")
    ):
        return None
    # The receipted ack needs the declared writable request point.
    if not any(
        point.get("id") == points["ack"] and point.get("writable")
        for point in model.get("io_points", [])
    ):
        return None
    return {
        "points": points,
        "table": table,
        "on_bad_demand": on_bad,
    }


def advance_demand(held, level, table):
    """The threshold chain's one-scan demand rule on a Good level —
    the declared table the leg recomputes against: the stop bound
    clears the demand outright, the lag bound lifts it to two, the
    start bound lifts an idle chain to one, and the hysteresis band
    carries the held stage."""
    if level <= table["stop"]:
        return 0
    if held == 2 and level <= table["start"]:
        return 1
    if level >= table["lag_start"]:
        return 2
    if held == 0 and level >= table["start"]:
        return 1
    return held


def measurement_tick(rig, failures):
    """One driven pair tick returning the field owner's snapshot."""
    return rig.tick(rig.standby_url, rig.duty_url, failures)[1]


def drive_until(rig, failures, condition):
    """Driven pair ticks until `condition(owner_snapshot)` holds —
    returns the satisfying snapshot, or None when MEASUREMENT_BOUND
    scans pass without it landing; the bound names a landing that
    never did."""
    for _ in range(MEASUREMENT_BOUND):
        owner = measurement_tick(rig, failures)
        if condition(owner):
            return owner
    return None


def inject(plant_io, point, fault, failures, naming):
    """One fault injection through the plant protocol's unfenced
    surface — `fault` the injected shape, None clearing the standing
    fault."""
    op = {"op": "inject_fault", "point": point, "fault": fault}
    if fault is None:
        op = {"op": "clear_fault", "point": point}
    verdict = plant_io.request(op)
    if verdict.get("result") != "done":
        failures.append(f"{naming} answered {verdict}")
        raise Abort


def heal_stored(plant_io, owner_token, failures, naming):
    """Repair the stored samples the injected quality latched into
    the dynamics' feedback elements: a quality fault's observed
    non-Good propagates through the elements into their stored
    samples — the simulated loop keeps serving Bad after the fault
    itself clears — so restoring a source is the `clear_fault` plus
    a same-value `write` on every plant-served point still reporting
    non-Good, the field's own publish of the restored instrument.
    The stored value rides back verbatim, so no process step hides
    in the repair, and the writes land inside the standing writer
    claim where the duty's recorded token names one — the shared
    claim path the scenario legs take for field-side state."""
    if owner_token is not None:
        verdict = plant_io.request(
            {"op": "ensure_writer", "owner": owner_token}
        )
        if verdict.get("result") not in ("done", "claimed_shared"):
            failures.append(
                f"{naming} writer-claim join answered {verdict}"
            )
            raise Abort
    census = plant_io.request({"op": "list_points"})
    census_points = (
        census.get("points") if isinstance(census, dict) else None
    )
    if census_points is None:
        failures.append(f"list_points answered {census}")
        raise Abort
    healed = []
    for info in census_points:
        served = info.get("sample") or {}
        if quality_key(served) == "good" or served.get("value") is None:
            continue
        verdict = plant_io.request(
            {
                "op": "write",
                "point": info["point"],
                "value": served["value"],
            }
        )
        if verdict.get("result") != "done":
            failures.append(
                f"{naming} write on point {info['point']} answered "
                f"{verdict}"
            )
            raise Abort
        healed.append(info["point"])
    return healed


def submit_command(url, command, failures):
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


def measurement_run(args, declared, wiring, tamper, failures):
    """The measurement-failover run: with the pair settled and
    tracking, the primary's field-channel quality fault fails the
    selection over to the backup — the backup-serving flag, the
    station still controlling on the selected measurement, the
    managed alarm's journaled annunciation; the backup joining it
    engages the declared all-sources-bad fallback rather than
    control on bad data; the restores return the selection to the
    primary and the alarm per its declared lifecycle, the pair's
    roles never moving. Returns `(digest_entries, evidence)`."""
    points = wiring["points"]
    table = wiring["table"]
    on_bad = wiring["on_bad_demand"]
    true, false = {"bool": True}, {"bool": False}
    digest_entries, evidence = [], {}
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        plant_io = rig.plant_io
        # The field owner's recorded writer-claim token — the stored
        # repairs join the standing claim under it where the release
        # records one.
        duty_token = owner_token(rig.duty_preamble)

        # Converge — the settled, tracking pair the exercise walks
        # from, each driven tick asserting identical images.
        converged = rig.converge(failures)
        owner = converged["owner"]
        evidence["measured_converged"] = converged["ticks"][-1]
        digest_entries.append(
            {
                "phase": "measurement_converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )

        # The healthy baseline: both sources Good, the primary
        # serving, the managed alarm quiet.
        primary_s = sample(owner, points["primary"])
        backup_s = sample(owner, points["backup"])
        out_s = sample(owner, points["out"])
        if not (
            primary_s is not None
            and backup_s is not None
            and out_s is not None
            and quality_key(primary_s) == "good"
            and quality_key(backup_s) == "good"
            and quality_key(out_s) == "good"
            and out_s.get("value") == primary_s.get("value")
            and value(owner, points["backup_active"]) == false
            and value(owner, points["alarm"]) == false
            and value(owner, points["unack"]) == false
        ):
            failures.append(
                "the converged pair is not on the healthy baseline — "
                f"primary {primary_s}, backup {backup_s}, out "
                f"{out_s}, backup_active "
                f"{value(owner, points['backup_active'])}, alarm "
                f"{value(owner, points['alarm'])}, unacknowledged "
                f"{value(owner, points['unack'])}"
            )
            raise Abort

        # The primary's field channel degrades — the unfenced
        # plant-protocol quality fault the scenario legs drive.
        inject(
            plant_io,
            points["primary"],
            {"quality": BAD_QUALITY},
            failures,
            "the primary's quality fault",
        )

        def failed_over(snapshot):
            primary_s = sample(snapshot, points["primary"])
            backup_s = sample(snapshot, points["backup"])
            out_s = sample(snapshot, points["out"])
            if primary_s is None or backup_s is None or out_s is None:
                return False
            if tamper == "controls-on-bad":
                # The doctored expectation: the degraded primary
                # still serving, the backup-serving flag down.
                return (
                    out_s.get("value") == primary_s.get("value")
                    and quality_key(out_s) == quality_key(primary_s)
                    and value(snapshot, points["backup_active"])
                    == false
                )
            return (
                quality_key(primary_s) == "bad:device_fault"
                and quality_key(backup_s) == "good"
                and quality_key(out_s) == "good"
                and out_s.get("value") == backup_s.get("value")
                and value(snapshot, points["backup_active"]) == true
                and value(snapshot, points["carrier"]) == true
                and value(snapshot, points["alarm"]) == true
                and value(snapshot, points["unack"]) == true
                and demand_stage(snapshot, points["demand"])
                is not None
            )

        owner = drive_until(rig, failures, failed_over)
        if owner is None:
            owner = measurement_tick(rig, failures)
            if tamper == "controls-on-bad":
                failures.append(
                    "the selection moved the station onto the "
                    "healthy backup — expected the station still "
                    "controlling on the bad primary"
                )
            else:
                failures.append(
                    "the bad primary never failed over — out reads "
                    f"{sample(owner, points['out'])}, backup_active "
                    f"{value(owner, points['backup_active'])}, "
                    f"carrier "
                    f"{value(owner, points['carrier'])}, alarm "
                    f"{value(owner, points['alarm'])}, "
                    f"unacknowledged "
                    f"{value(owner, points['unack'])}"
                )
            raise Abort
        evidence["annunciated_at"] = owner["tick"]

        # The backup-serving control window: each driven tick the
        # selection serves the backup's sample at Good, the chain's
        # delivered level carries the previous selection — the mirror
        # `In` point's declared one-scan hop — and the demand advances
        # per the declared table: the station controlling on the
        # selected measurement, never frozen and never the
        # untrusted-input fallback while the level reads healthy.
        controlled = []
        held = demand_stage(owner, points["demand"])
        prev_out = sample(owner, points["out"])
        for _ in range(CONTINUE_TICKS):
            owner = measurement_tick(rig, failures)
            backup_s = sample(owner, points["backup"])
            out_s = sample(owner, points["out"])
            level_s = sample(owner, points["level"])
            stage = demand_stage(owner, points["demand"])
            if not (
                backup_s is not None
                and out_s is not None
                and quality_key(out_s) == "good"
                and out_s.get("value") == backup_s.get("value")
                and prev_out is not None
                and level_s is not None
                and quality_key(level_s) == "good"
                and level_s.get("value") == prev_out.get("value")
                and stage is not None
            ):
                failures.append(
                    "the station is not controlling on the backup "
                    f"value — out {out_s}, delivered level "
                    f"{level_s}, previous selection {prev_out}, "
                    f"backup {backup_s}"
                )
                raise Abort
            level_v = level_s["value"]["float"]
            want = advance_demand(held, level_v, table)
            if stage != want:
                failures.append(
                    f"demand {stage} is not the declared evaluation "
                    f"{want} of the backup-serving level {level_v} — "
                    "the chain is not controlling on the selected "
                    "measurement"
                )
                raise Abort
            controlled.append(
                {
                    "tick": owner["tick"],
                    "level": level_v,
                    "demand": stage,
                }
            )
            prev_out = out_s
            held = stage
        digest_entries.append(
            {
                "phase": "backup_serving",
                "annunciated_at": evidence["annunciated_at"],
                "controlled": controlled,
            }
        )

        # The backup joins the primary — every source untrusted, the
        # selection drops to Bad and the chain emits the declared
        # on_bad_demand rather than controlling on bad data, the
        # backup-serving flag's alarm still standing.
        inject(
            plant_io,
            points["backup"],
            {"quality": BAD_QUALITY},
            failures,
            "the backup's quality fault",
        )

        def all_bad(snapshot):
            out_s = sample(snapshot, points["out"])
            level_s = sample(snapshot, points["level"])
            stage = demand_stage(snapshot, points["demand"])
            if (
                out_s is None
                or quality_key(out_s) == "good"
                or level_s is None
                or quality_key(level_s) == "good"
                or stage is None
            ):
                return False
            if tamper == "nonzero-fallback":
                if stage == on_bad:
                    return False
            elif stage != on_bad:
                return False
            return (
                value(snapshot, points["backup_active"]) == true
                and value(snapshot, points["alarm"]) == true
                and value(snapshot, points["unack"]) == true
            )

        owner = drive_until(rig, failures, all_bad)
        if owner is None:
            owner = measurement_tick(rig, failures)
            if tamper == "nonzero-fallback":
                failures.append(
                    "the fallback demand dropped to the declared "
                    f"on_bad_demand {on_bad} — expected the fallback "
                    "demand nonzero"
                )
            else:
                failures.append(
                    "the all-bad state never engaged the declared "
                    f"fallback — out reads "
                    f"{sample(owner, points['out'])}, delivered "
                    f"level {sample(owner, points['level'])}, "
                    f"demand {sample(owner, points['demand'])}; "
                    f"expected an untrusted selection and demand "
                    f"{on_bad}"
                )
            raise Abort
        evidence["fallback_at"] = owner["tick"]

        # The bad window's persistence: the selection and the
        # delivered level stay untrusted, the demand pinned at the
        # declared safe value — the station does not control on the
        # bad data.
        bad_ticks = []
        for _ in range(CONTINUE_TICKS):
            owner = measurement_tick(rig, failures)
            out_s = sample(owner, points["out"])
            level_s = sample(owner, points["level"])
            stage = demand_stage(owner, points["demand"])
            if not (
                out_s is not None
                and quality_key(out_s) != "good"
                and level_s is not None
                and quality_key(level_s) != "good"
                and stage == on_bad
            ):
                failures.append(
                    "the station controlled on the bad data — out "
                    f"{out_s}, delivered level {level_s}, demand "
                    f"{sample(owner, points['demand'])}; expected "
                    f"an untrusted selection and the declared "
                    f"on_bad_demand {on_bad}"
                )
                raise Abort
            bad_ticks.append(owner["tick"])
        digest_entries.append(
            {
                "phase": "all_bad",
                "fallback_at": evidence["fallback_at"],
                "on_bad_demand": on_bad,
                "ticks": bad_ticks,
            }
        )

        # Restore the backup first — the injected fault clears, then
        # the stored repair republishes the points its Bad quality
        # latched; the backup resumes serving at Good while the
        # primary stays faulted, the flag and its alarm still
        # standing, the chain evaluating again from the fallback
        # stage.
        inject(
            plant_io,
            points["backup"],
            None,
            failures,
            "the backup's fault clear",
        )
        healed = heal_stored(
            plant_io,
            duty_token,
            failures,
            "the backup's stored repair",
        )

        def backup_back(snapshot):
            backup_s = sample(snapshot, points["backup"])
            out_s = sample(snapshot, points["out"])
            return (
                backup_s is not None
                and quality_key(backup_s) == "good"
                and out_s is not None
                and quality_key(out_s) == "good"
                and out_s.get("value") == backup_s.get("value")
                and value(snapshot, points["backup_active"]) == true
                and value(snapshot, points["alarm"]) == true
                and value(snapshot, points["unack"]) == true
            )

        owner = drive_until(rig, failures, backup_back)
        if owner is None:
            owner = measurement_tick(rig, failures)
            failures.append(
                "the restored backup never resumed serving — out "
                f"reads {sample(owner, points['out'])}, "
                f"backup_active "
                f"{value(owner, points['backup_active'])}"
            )
            raise Abort
        evidence["backup_restored_at"] = owner["tick"]

        held = on_bad
        resumed = []
        for _ in range(CONTINUE_TICKS):
            owner = measurement_tick(rig, failures)
            level_s = sample(owner, points["level"])
            stage = demand_stage(owner, points["demand"])
            if not (
                level_s is not None
                and quality_key(level_s) == "good"
                and stage is not None
            ):
                failures.append(
                    "the restored backup is not serving the chain — "
                    f"delivered level {level_s}, demand "
                    f"{sample(owner, points['demand'])}"
                )
                raise Abort
            level_v = level_s["value"]["float"]
            want = advance_demand(held, level_v, table)
            if stage != want:
                failures.append(
                    f"demand {stage} is not the declared evaluation "
                    f"{want} of the restored level {level_v} — the "
                    "chain did not resume on the serving "
                    "measurement"
                )
                raise Abort
            resumed.append(
                {
                    "tick": owner["tick"],
                    "level": level_v,
                    "demand": stage,
                }
            )
            held = stage
        digest_entries.append(
            {
                "phase": "backup_restored",
                "restored_at": evidence["backup_restored_at"],
                "healed": healed,
                "resumed": resumed,
            }
        )

        # Restore the primary — the injected fault clears and the
        # stored repair republishes whatever the loop still latches;
        # the same-scan reselection the stateless selector declares:
        # the primary serves again, the flag and the alarm return,
        # the unacknowledged latch standing for its receipted ack.
        inject(
            plant_io,
            points["primary"],
            None,
            failures,
            "the primary's fault clear",
        )
        heal_stored(
            plant_io,
            duty_token,
            failures,
            "the primary's stored repair",
        )

        def primary_back(snapshot):
            primary_s = sample(snapshot, points["primary"])
            out_s = sample(snapshot, points["out"])
            return (
                primary_s is not None
                and quality_key(primary_s) == "good"
                and out_s is not None
                and quality_key(out_s) == "good"
                and out_s.get("value") == primary_s.get("value")
                and value(snapshot, points["backup_active"]) == false
                and value(snapshot, points["alarm"]) == false
                and value(snapshot, points["unack"]) == true
            )

        owner = drive_until(rig, failures, primary_back)
        if owner is None:
            owner = measurement_tick(rig, failures)
            failures.append(
                "the restored primary never resumed serving, or the "
                "alarm did not return per its declared lifecycle — "
                f"out {sample(owner, points['out'])}, backup_active "
                f"{value(owner, points['backup_active'])}, alarm "
                f"{value(owner, points['alarm'])}, unacknowledged "
                f"{value(owner, points['unack'])}; expected the "
                "primary serving, the alarm returned, the latch "
                "standing"
            )
            raise Abort
        evidence["reselected_at"] = owner["tick"]

        # The receipted acknowledgment — the lifecycle's closing
        # edge: the standing latch clears on the applied write, the
        # released ack journaled too.
        ack_write = managed_lifecycle.write_value(
            points["ack"], True
        )
        submit_command(duty_url, ack_write, failures)
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: value(snapshot, points["unack"]) == false,
        )
        if owner is None:
            owner = measurement_tick(rig, failures)
            failures.append(
                "the receipted ack never cleared the standing latch "
                f"— unacknowledged "
                f"{value(owner, points['unack'])}"
            )
            raise Abort
        receipts = pair.get(
            f"{duty_url}/receipts", "GET /receipts", failures
        )
        if not managed_lifecycle.settled(receipts, ack_write):
            failures.append(
                "the ack write never settled applied into the "
                "adopted receipt log"
            )
            raise Abort
        evidence["acked_at"] = owner["tick"]
        ack_release = managed_lifecycle.write_value(
            points["ack"], False
        )
        submit_command(duty_url, ack_release, failures)
        owner = measurement_tick(rig, failures)
        receipts = pair.get(
            f"{duty_url}/receipts", "GET /receipts", failures
        )
        if not managed_lifecycle.settled(receipts, ack_release):
            failures.append(
                "the ack release never settled applied into the "
                "adopted receipt log"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "primary_restored",
                "reselected_at": evidence["reselected_at"],
                "acked_at": evidence["acked_at"],
            }
        )

        # Roles unchanged — the duty still the field owner, the
        # standby still tracking.
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        standby_role = pair.get(
            f"{standby_url}/role", "GET /role", failures
        )
        if (
            duty_role.get("role") != "active"
            or standby_role.get("role") != "standby"
        ):
            failures.append(
                f"the pair's roles moved — duty {duty_role}, "
                f"standby {standby_role}"
            )
            raise Abort
        evidence["measured_duty_role"] = duty_role.get("role")
        evidence["measured_standby_role"] = standby_role.get("role")

        # The durable journal: the failover's transitions in driven
        # order, the served journal matching the durable one.
        journal_path = rig.duty_files.get("journal_file")
        if journal_path is None or not os.path.exists(journal_path):
            failures.append(
                "the field owner's declared journal file does not "
                "exist — the --journal-file flag was not honored"
            )
            raise Abort
        entries = [
            record
            for kind, record in pair.journal_records(journal_path)
            if kind == "entry"
        ]
        events = managed_lifecycle.journal_events(entries)
        groups = [
            # The primary's fault and the failover's annunciation —
            # the journaled `point_changed` evidence the managed
            # alarm's transition carries.
            [
                ("quality", points["primary"], BAD_QUALITY),
                ("changed", points["backup_active"], true),
                ("changed", points["alarm"], true),
                ("changed", points["unack"], true),
            ],
            # The backup joining it — the all-bad engagement
            # journaled through the source's own quality record.
            [("quality", points["backup"], BAD_QUALITY)],
            # The backup's restore — quality back to Good while the
            # primary still serves nothing.
            [("quality", points["backup"], "good")],
            # The primary's restore — the selection and the
            # standing alarm return, the latch holding.
            [
                ("quality", points["primary"], "good"),
                ("changed", points["backup_active"], false),
                ("changed", points["alarm"], false),
            ],
            # The receipted ack clearing the latch, then released.
            [
                ("settled", points["ack"], true, "applied", ACTOR),
                ("changed", points["unack"], false),
                ("settled", points["ack"], false, "applied", ACTOR),
            ],
        ]
        failures.extend(
            managed_lifecycle.ordered_group_misses(events, groups)
        )
        if failures:
            raise Abort
        served = pair.get(
            f"{duty_url}/journal", "GET /journal", failures
        )
        if managed_lifecycle.journal_events(served) != events:
            failures.append(
                "the served journal's transition stream diverges "
                "from the durable file's — the monitor does not "
                "answer the record it persists"
            )
            raise Abort
        digest_entries.append(
            {"phase": "measurement_audit", "events": events}
        )
        evidence["measured_entries"] = len(entries)
    finally:
        if rig is not None:
            rig.close()
    return digest_entries, evidence


def failover_pass(args, tamper):
    """The failover run: the severed-owner self-promotion, the
    severed-standby no-failover variant, then the measurement
    failover and all-sources-bad fallback exercise. Returns
    `(digest_entries, evidence, failures)`."""
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
    wiring = measurement_wiring(model)
    if wiring is None:
        raise Abort(
            "the emitted model declares no measurement-failover seam "
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
        measured, measured_evidence = measurement_run(
            args, declared, wiring, tamper, failures
        )
        digest_entries.extend(measured)
        evidence.update(measured_evidence)
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
        choices=["early-promotion", "controls-on-bad", "nonzero-fallback"],
        help="doctor the leg's own expectation — the pass must fail "
        "naming the evidence",
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
        f"{evidence['undisturbed_tick']}; the measurement failover "
        f"annunciated at tick {evidence['annunciated_at']}, fell back "
        f"to the declared safe demand at tick "
        f"{evidence['fallback_at']}, and re-selected the primary at "
        f"tick {evidence['reselected_at']} with the ack settled at "
        f"tick {evidence['acked_at']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
