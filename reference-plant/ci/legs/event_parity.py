#!/usr/bin/env python3
"""The emit-identical standby event-parity leg for the reference
plant — the consumer-side proof that a tracking standby publishes the
same routed emitted-event records as the active (WW-ENG-003,
WW-FND-003, WW-LCM-001 — decision 84's emit-identical tracking).

The pair leg (`ci/legs/pair.py`) proves the manifest-declared pair runs and
switches bumplessly; the command-switch leg proves declared commands
and emitted events continue across promotion. This leg proves the
read-model parity decision 84 pins workspace-side (#381) on the
customer's deployed pair: the tracking standby re-derives the same
routed emitted events from adopted state as the field owner, so its
published per-instance resource view carries the same records rather
than a hollow or diverged stream — the property that lets a UI
consumer fail its event feed over between the peers without a gap in
attribution.

The proving kind is the emitted model's `sequencer` — the served
registry's first kind-declared event, `step_completed`. The run:

- converges the declared standby to `tracking` through the pair leg's
  driven-tick loop;
- holds the emitter's writable boolean `run` input through the field
  owner's receipted `POST /command` path — the same write refused
  `not_active` at the standby's role boundary, the pair's
  writes-stay-gated half;
- drives the declared step table out through tracking-first pair
  ticks — each `POST /scan` pulling and applying the owner's
  checkpoint before the tracking peer re-derives the tick's emissions
  — until the counted `step_completed` set stands;
- asserts both peers' `GET /resources` views collect the same routed
  `event_emitted` records: identical outer resource attribution and
  inner producer component, declared event identities, ordered payload
  fields, tick, and retention — the stream-local `seq` positions
  excluded, each peer's store numbering its own — while the standby
  still reports `tracking` and the owner `active`.

Usage:

    event_parity.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `event-parity-digest <sha256>` line prints — the check
runs two passes and compares them (`event-parity-nondeterministic`). A
contract violation reports `event-parity: …` lines on stderr and exits
1 — the check's `event-parity-failed`. The `--tamper` choices doctor
the standby's served resource view: `dropped-event-record` removes an
emission record and `reattributed-event-record` re-attributes one, so
the leg proves its parity assertion fires — each must report
`event-parity-failed`.
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

import pair
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored cases: a standby whose served record set is
# missing an emission or carries one re-attributed must surface
# the named diagnostic — never a silently hollow or diverged
# parity pass.
LEG = {
    "order": 220,
    "title": "the standby event-parity leg",
    "passes": "event-parity",
    "tampers": [
        {
            "name": "dropped-event-record",
            "passed": "the dropped-event-record case passed the parity leg",
            "missed": "the dropped-event-record case did not report event-parity-failed",
            "evidence": ["event-parity-failed"],
        },
        {
            "name": "reattributed-event-record",
            "passed": "the reattributed-event-record case passed the parity leg",
            "missed": "the reattributed-event-record case did not report event-parity-failed",
            "evidence": ["event-parity-failed"],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The actor the leg's receipted submissions declare.
ACTOR = "ci-event-parity"


def event_records(view):
    """The routed `event_emitted` records a served `GET /resources`
    view carries — every component's `events` in served order,
    projected to the comparable record: the outer resource `name` the
    emission attributes under as `component`, `tick` and `retention`
    carried, and the full inner EmittedEvent with `fields` the ordered
    item list. The entry's stream-local `seq` and the view's
    `publication` marker are excluded — local stream positions, not
    routed identity."""
    records = []
    for component in view.get("components", []):
        for entry in component.get("events", []):
            emitted = (
                entry.get("event", {}).get("event_emitted", {}).get("event")
            )
            if not isinstance(emitted, dict):
                continue
            records.append(
                {
                    "component": component.get("name"),
                    "tick": entry.get("tick"),
                    "retention": entry.get("retention", "journal"),
                    "event": dict(
                        emitted,
                        fields=list(emitted.get("fields", {}).items()),
                    ),
                }
            )
    return records


def record_divergence(records, expected):
    """The first difference between a peer's collected records and the
    expected set — the mismatched record's served and expected forms,
    or the first expected record a shorter stream dropped."""
    for index, (got, want) in enumerate(zip(records, expected)):
        if got != want:
            return (
                f"record {index} serves "
                f"{json.dumps(got, sort_keys=True, default=str)}, "
                f"expected {json.dumps(want, sort_keys=True, default=str)}"
            )
    if len(records) < len(expected):
        tail = json.dumps(expected[len(records):], sort_keys=True, default=str)
        return (
            f"{len(records)} records against the expected "
            f"{len(expected)} — missing {tail}"
        )
    if len(records) > len(expected):
        tail = json.dumps(records[len(expected):], sort_keys=True, default=str)
        return (
            f"{len(records)} records against the expected "
            f"{len(expected)} — extra {tail}"
        )
    return "the streams differ"


def assert_event_parity(active_view, standby_view, expected):
    """Decision 84's emit-identical rule at the served surface: both
    peers' collected `event_emitted` records must equal the expected
    counted set — identical component attribution, declared
    identities, ordered fields, tick, and retention, the stream-local
    seqs excluded. A divergence raises Abort carrying
    `event-parity-failed` and the first difference."""
    for label, view in (("active", active_view), ("standby", standby_view)):
        records = event_records(view)
        if records != expected:
            raise Abort(
                f"event-parity-failed: the {label} peer's served "
                f"emissions diverge — {record_divergence(records, expected)}"
            )
    return expected


def doctor_view(view, tamper):
    """Apply a parity tamper to a fetched served resource view — the
    doctored standby whose missing or re-attributed emission record
    the assertion must catch."""
    for component in view.get("components", []):
        events = component.get("events", [])
        for index, entry in enumerate(events):
            if "event_emitted" not in entry.get("event", {}):
                continue
            if tamper == "dropped-event-record":
                del events[index]
            elif tamper == "reattributed-event-record":
                entry["event"]["event_emitted"]["event"]["component"] = (
                    "doctored:0"
                )
            return


def parity_pass(args, tamper):
    """The emit-identical run: converge, drive the counted emission
    set, assert both peers serve the same routed records. Returns
    `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the event-parity "
            "leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    with open(args.model) as handle:
        model = json.load(handle)
    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared, None)
        duty_url, standby_url = rig.duty_url, rig.standby_url

        # Phase 1 — the same convergence the switchover leg proves:
        # the tracking peer scans first each tick until GET /role
        # reports it `tracking`.
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

        # Phase 2 — the counted emission set. The served registry's
        # first kind-declared event names the emitter; its component's
        # writable boolean `run` input is held through the field
        # owner's receipted path — refused `not_active` at the
        # standby's role boundary, the pair's writes-stay-gated half —
        # and the declared step table's scans run out, each tick
        # having the tracking peer re-derive the same emissions from
        # the adopted checkpoint.
        schema = pair.get(f"{duty_url}/schema", "GET /schema", failures)
        events = simulate.declared_events(schema)
        if not events:
            failures.append(
                "event-parity-failed: the served registry declares no "
                "kind-emitted event — the leg has no counted set to "
                "drive"
            )
            raise Abort
        component, spec = events[0]
        bound = simulate.bound_points(model)
        writable = {
            entry["id"]
            for entry in model["io_points"]
            if entry.get("writable")
        }
        point = bound.get((int(component.rsplit(":", 1)[1]), "run"))
        if point is None or point not in writable:
            failures.append(
                f"event-parity-failed: {component} binds no writable "
                f"`run` input to drive {spec['name']} — the leg has "
                "no counted set to drive"
            )
            raise Abort
        command = {
            "write_value": {
                "kind": "bool",
                "point": point,
                "value": {"bool": True},
            }
        }
        status, receipt = pair.request(
            f"{duty_url}/command", {"command": command, "actor": ACTOR}
        )
        if status != 200 or simulate.receipt_outcome(receipt) != "accepted":
            failures.append(
                f"the `run` write driving {component}'s {spec['name']} "
                f"answered {status} {receipt}, expected an accepted "
                "receipt"
            )
            raise Abort
        status, refusal = pair.request(
            f"{standby_url}/command", {"command": command, "actor": ACTOR}
        )
        reason = (
            refusal.get("outcome", {}).get("rejected", {}).get("reason", {})
            if isinstance(refusal, dict)
            else {}
        )
        if status != 200 or "not_active" not in reason:
            failures.append(
                f"the standby's role boundary answered {status} "
                f"{refusal}, expected a rejected not_active receipt — "
                "the tracking peer's writes must stay gated"
            )
            raise Abort

        scans = simulate.emission_scans(model, component)
        if scans <= 0:
            failures.append(
                f"event-parity-failed: {component}'s declared step "
                "table drives no emission — the leg has no counted set"
            )
            raise Abort
        # The emission drive: tracking-first pair ticks run the table
        # out — the carried write's one-tick lag absorbed by `tick`'s
        # healing pass — while the tracking peer re-derives each
        # tick's emissions from the adopted checkpoint.
        ticks = []
        for _ in range(scans):
            _tracked, owner = rig.tick(
                standby_url,
                duty_url,
                failures,
                diverged="the tracking peer's image diverged from the "
                "field owner's at tick {tick} — the adopted state no "
                "longer tracks",
            )
            ticks.append(owner["tick"])
        digest_entries.append(
            {
                "phase": "emissions",
                "component": component,
                "event": spec["name"],
                "receipt": receipt,
                "refusal": refusal,
                "ticks": ticks,
            }
        )

        # Phase 3 — the parity itself. The roles must not have moved —
        # the tracking peer still standby, the owner still active —
        # then both peers' served resource views collect their routed
        # `event_emitted` records and the standby's must equal the
        # active's: same component attribution, declared identities,
        # ordered fields, tick, and retention.
        standby_role = pair.get(
            f"{standby_url}/role", "GET /role", failures
        )
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        sync = standby_role.get("sync")
        if standby_role.get("role") != "standby" or not (
            isinstance(sync, dict) and "tracking" in sync
        ):
            failures.append(
                "the tracking peer left its standby role across the "
                f"emission drive — GET /role answers {standby_role}"
            )
            raise Abort
        if duty_role.get("role") != "active":
            failures.append(
                f"the field owner reports {duty_role.get('role')!r} "
                "across the emission drive, expected active"
            )
            raise Abort
        duty_view = pair.get(
            f"{duty_url}/resources", "GET /resources", failures
        )
        standby_view = pair.get(
            f"{standby_url}/resources", "GET /resources", failures
        )
        expected = event_records(duty_view)
        counted = [
            record
            for record in expected
            if record["component"] == component
            and record["event"]["event"] == spec["name"]
        ]
        declared_component = next(
            (
                entry
                for entry in model["components"]
                if f"{entry['kind']}:{entry['id']}" == component
            ),
            {},
        )
        steps = sum(
            1
            for name in declared_component.get("parameters", {})
            if re.fullmatch(r"step_\d+_ticks", name)
        )
        if not counted:
            failures.append(
                f"event-parity-failed: the active serves no emitted "
                f"{spec['name']} record from {component} — the counted "
                "set never stood"
            )
            raise Abort
        if steps and len(counted) != steps:
            failures.append(
                f"event-parity-failed: the active serves "
                f"{len(counted)} {spec['name']} records from "
                f"{component} against the declared step table's {steps}"
            )
            raise Abort
        doctor_view(standby_view, tamper)
        records = assert_event_parity(duty_view, standby_view, expected)
        digest_entries.append(
            {
                "phase": "parity",
                "component": component,
                "event": spec["name"],
                "counted": len(counted),
                "records": records,
                "duty_role": duty_role,
                "standby_role": standby_role,
            }
        )
        evidence["final_tick"] = ticks[-1]
        evidence["counted"] = len(counted)
        evidence["event"] = spec["name"]
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
        choices=["dropped-event-record", "reattributed-event-record"],
        help="doctor the standby's served record set — the pass must "
        "fail naming the parity evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = parity_pass(args, args.tamper)
    except Abort as abort:
        for line in abort.args:
            eprint(f"event-parity: {line}")
        return 1
    for failure in failures:
        if failure.startswith("event-parity-failed:"):
            eprint(failure)
        else:
            eprint(f"event-parity: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"event-parity: the {args.tamper} case passed silently "
                "— the leg never noticed the doctored record set"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"event-parity-digest {digest} — {evidence['counted']} "
        f"counted {evidence['event']} records identical on both "
        f"peers, the standby tracking at tick "
        f"{evidence['final_tick']} (converged at "
        f"{evidence['converged']})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
