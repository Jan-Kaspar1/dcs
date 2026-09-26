#!/usr/bin/env python3
"""The attributed role-switch leg for the reference plant — the
consumer-side proof that the manifest-declared pair's durable record
names *who asked* on the most consequential action the pair surface
exposes: an operator-requested `POST /promote` / `POST /demote`
switch is journaled carrying the request's declared actor, while the
executor's own automatic failover records a distinguishable
non-operator marker rather than reading as an unattributed request
(WW-ENG-003, WW-OPS-002).

The pair leg proves the receipted demote/promote switch holds the
field bumpless; the failover leg proves the armed standby
self-promotes at its declared miss budget. This leg proves the
journal's *attribution* semantics on the deployed consumer pair —
the actor convention the switch endpoints landed under #467, carried
into the durable `--journal-file` the consumer keeps. The run:

- converges the declared standby to `tracking` on the armed pair —
  the manifest's `failover_budget` carried to `--auto-promote`;
- submits an attributed `POST /demote {"actor": "ci"}` against the
  field owner then an attributed `POST /promote {"actor": "ci"}`
  against the tracking standby — each through the serving monitor,
  each answered by its `RoleReport` — then drives the handover;
- asserts on *both* peers' durable journals that the switch's
  `role_changed` entries carry `origin: "request"` and the declared
  actor `ci`, the request transition attributed at the request's
  scan boundary (the peer's tick as the request landed) and the
  settle transition at the following one — ordered strictly after
  the pre-request record — and that the tracking peer's
  checkpoint-adopted journal shows the same attributed record: a
  receipted command settled on the promoted owner journals its
  `command_settled` on the demoted tracker with the declared actor
  carried over the checkpoint — the carryover the pair legs already
  prove;
- exercises the attributed `POST /demote`/`POST /promote` pair the
  same way back — the fail-back switch restoring the launch roles
  with the same durable attribution on both peers;
- drives the automatic half: the field owner is stopped — its
  checkpoint serving severed — and the surviving standby's driven
  scans run its consecutive miss count to the declared budget, whose
  expiry's scan boundary self-promotes it; the durable record must
  carry the promotion's transitions under `origin: "failover"` with
  no operator actor — reading distinguishably from any operator
  request, attributed or not;
- restores the pair: the duty peer is respawned on its declared
  listen address tracking the promoted peer, reconverges
  `tracking`, and an attributed demote/promote pair returns the
  launch roles — the respawn binding the same address so the
  demoted peer's configured `--standby` resolves to it.

The named diagnostics: a contract violation exits 1 — the check's
`attribution-failed`; two passes producing different digests fails
`attribution-nondeterministic`. The doctored cases prove the audits
fire: `missing-actor` issues the switch requests unattributed — the
durable record's missing declared actor must surface — and
`failover-as-request` doctors the leg's expectation to demand the
self-promotion's record reading as an operator request, so a release
misclassifying the automatic path could not hide behind the audit.

Usage:

    attribution.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `attribution-digest <sha256>` line prints — the
check runs two passes and compares them.
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


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
LEG = {
    "order": 145,
    "title": "the attributed role-switch leg",
    "passes": "attribution-leg",
    "tampers": [
        {
            "name": "missing-actor",
            "passed": "an unattributed switch passed the attribution leg",
            "missed": "the missing-actor case did not report its named diagnostic",
            "evidence": ["carrying the declared actor"],
        },
        {
            "name": "failover-as-request",
            "passed": "a failover read as an operator request passed the attribution leg",
            "missed": "the failover-as-request case did not report its named diagnostic",
            "evidence": ["reading as an operator request"],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The declared actor every attributed request carries — the
# attestation identity the durable records must name. The driven
# ticks each phase runs.
ACTOR = "ci"
HANDOVER_TICKS = pair.HANDOVER_TICKS
CONTINUE_TICKS = 4
TRACK_BOUND = 8


def role_changes(entries):
    """The `role_changed` stream of a journal entry list carrying
    its full attribution — `(tick, from, to, origin, actor)` per
    entry, in `seq` order."""
    return [
        (
            entry["tick"],
            change.get("from"),
            change.get("to"),
            change.get("origin"),
            change.get("actor"),
        )
        for entry in entries
        for change in [entry.get("event", {}).get("role_changed")]
        if change is not None
    ]


def journal_entries(path):
    """The durable journal file's `(boundaries, entries)` —
    `pair.journal_records` split by kind."""
    records = pair.journal_records(path)
    return (
        [record for kind, record in records if kind == "boundary"],
        [record for kind, record in records if kind == "entry"],
    )


def durable_floor(path, name, failures):
    """The durable journal's standing depth — the count of `entry`
    records the run must have written before a switch's request, or
    a failure when the declared file is absent."""
    if not os.path.exists(path):
        failures.append(
            f"{name}'s declared journal file {path} does not exist "
            "— the --journal-file flag was not honored"
        )
        raise Abort
    return len(journal_entries(path)[1])


def role_floor(url, failures):
    """The peer's tick at the instant its switch request lands —
    the scan boundary the request transition attributes to."""
    return pair.get(f"{url}/role", "GET /role", failures)["tick"]


def switch_request(url, endpoint, actor, failures, what):
    """`POST /<endpoint>` carrying the declared actor — `None`
    submits the bare request — asserting the answered RoleReport
    names the transition."""
    body = {"actor": actor} if actor is not None else {}
    status, report = pair.request(f"{url}/{endpoint}", body)
    want = "demoting" if endpoint == "demote" else "promoting"
    if status != 200 or report.get("role") != want:
        failures.append(
            f"POST /{endpoint} on {what} answered {status} {report}, "
            f"expected a {want} report"
        )
        raise Abort
    return report


def audit_attributed(
    name, entries, floor, floor_tick, want, failures, actor=ACTOR
):
    """The peer's gained durable `role_changed` stream must be
    exactly the switch's two transitions — request then settle —
    each carrying `origin: "request"` and the declared actor, the
    request entry attributed at the request's scan boundary and the
    settle at the next. Returns the gained stream for the digest."""
    gained = role_changes(entries[floor:])
    pairs = [(frm, to) for _tick, frm, to, _origin, _actor in gained]
    if pairs != want:
        failures.append(
            f"{name}'s durable journal gained the role transitions "
            f"{gained} across the attributed switch, expected {want}"
        )
        return gained
    ticks = [tick for tick, _frm, _to, _origin, _actor in gained]
    if ticks != [floor_tick, floor_tick + 1]:
        failures.append(
            f"{name}'s attributed transitions attribute at ticks "
            f"{ticks}, expected the request's scan boundary "
            f"{floor_tick} and its settle at {floor_tick + 1} — the "
            "record must order the switch after the request"
        )
    for tick, frm, to, origin, seen in gained:
        if origin != "request" or seen != actor:
            failures.append(
                f"{name}'s durable journal carries "
                f"{frm}->{to} at tick {tick} with origin={origin!r} "
                f"actor={seen!r}, expected role_changed entries "
                f"carrying the declared actor {actor!r} under origin "
                "'request' — an attributed operator switch must "
                "journal who asked"
            )
    return gained


def audit_adopted(entries, floor, command, name, failures, actor=ACTOR):
    """The tracking peer's checkpoint-adopted journal must show the
    same attributed record: the `command_settled` the owner's scan
    boundary journaled carries the declared actor over the
    checkpoint — the carryover the pair legs already prove. Returns
    the adopted receipt or None."""
    adopted = [
        receipt
        for entry in entries[floor:]
        for receipt in [
            entry.get("event", {})
            .get("command_settled", {})
            .get("receipt")
        ]
        if isinstance(receipt, dict)
        and receipt.get("command") == command
    ]
    if not adopted:
        failures.append(
            f"{name}'s checkpoint-adopted journal shows no record "
            "of the attributed command — the same attributed record "
            "the owner journaled must carry over the checkpoint"
        )
        return None
    for receipt in adopted:
        if receipt.get("actor") != actor:
            failures.append(
                f"{name}'s checkpoint-adopted receipt carries "
                f"actor={receipt.get('actor')!r}, expected the "
                f"adopted record carrying the declared actor "
                f"{actor!r}"
            )
    return adopted[-1]


def declared_invoke(url, failures):
    """One kind-declared `invoke` command body the served registry
    offers — the pair leg's command-resolution convention."""
    schema = pair.get(f"{url}/schema", "GET /schema", failures)
    declared = pair.declared_command(schema)
    if declared is None:
        failures.append(
            "the served registry declares no command — the "
            "attribution leg has no receipted path to exercise"
        )
        raise Abort
    component, spec = declared
    return {
        "invoke": {
            "component": component,
            "command": spec["name"],
            "arguments": simulate.command_arguments(spec),
        }
    }


def attributed_switch(
    rig,
    demote_url,
    promote_url,
    failures,
    actor,
    demote_what,
    promote_what,
):
    """One attributed switch: `POST /demote {"actor"}` on the field
    owner then `POST /promote {"actor"}` on the converged peer, the
    handover's driven ticks, and the durable-attribution audit on
    both peers. Returns the switch record for the digest."""
    demoted_name = rig.peer_name(demote_url)
    promoted_name = rig.peer_name(promote_url)
    files = {
        demoted_name: (
            rig.duty_files if demote_url == rig.duty_url else rig.standby_files
        ),
        promoted_name: (
            rig.duty_files
            if promote_url == rig.duty_url
            else rig.standby_files
        ),
    }
    floors = {}
    ticks = {}
    for name, url in (
        (demoted_name, demote_url),
        (promoted_name, promote_url),
    ):
        floors[name] = durable_floor(
            files[name]["journal_file"], name, failures
        )
        ticks[name] = role_floor(url, failures)

    demote = switch_request(
        demote_url, "demote", actor, failures, demote_what
    )
    promote = switch_request(
        promote_url, "promote", actor, failures, promote_what
    )

    handover = []
    for _ in range(HANDOVER_TICKS):
        _tracked, owner = rig.tick(demote_url, promote_url, failures)
        handover.append(owner["tick"])

    promoted_role = pair.get(
        f"{promote_url}/role", "GET /role", failures
    )
    demoted_role = pair.get(
        f"{demote_url}/role", "GET /role", failures
    )
    if promoted_role.get("role") != "active":
        failures.append(
            f"the promoted peer reports "
            f"{promoted_role.get('role')!r}, expected active"
        )
    sync = demoted_role.get("sync")
    if demoted_role.get("role") != "standby" or not (
        isinstance(sync, dict) and "tracking" in sync
    ):
        failures.append(
            "the demoted peer never reconverged — GET /role answers "
            f"{demoted_role}"
        )
    if failures:
        raise Abort

    attributions = {}
    for name, url, want in (
        (
            demoted_name,
            demote_url,
            [("active", "demoting"), ("demoting", "standby")],
        ),
        (
            promoted_name,
            promote_url,
            [("standby", "promoting"), ("promoting", "active")],
        ),
    ):
        _boundaries, entries = journal_entries(
            files[name]["journal_file"]
        )
        attributions[name] = audit_attributed(
            name, entries, floors[name], ticks[name], want, failures
        )
    if failures:
        raise Abort
    return {
        "demote": demote,
        "promote": promote,
        "ticks": handover,
        "demoted_role": demoted_role,
        "promoted_role": promoted_role,
        "attributions": attributions,
    }


def tracking(url, failures):
    """The peer's `standby`/`tracking` RoleReport, or None."""
    report = pair.get(f"{url}/role", "GET /role", failures)
    sync = report.get("sync")
    if report.get("role") == "standby" and (
        isinstance(sync, dict) and "tracking" in sync
    ):
        return report
    return None


def attribution_pass(args, tamper):
    """The attribution run: converge, two attributed switches, the
    armed standby's automatic failover, and the attributed restore.
    Returns `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "attribution leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    budget = standby_decl.get("failover_budget")
    if (
        not isinstance(budget, int)
        or isinstance(budget, bool)
        or budget < 1
    ):
        raise Abort(
            "the manifest's standby declares no positive "
            "failover_budget — the attribution leg has nothing to "
            "exercise"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = None
    # The doctored case: the switch requests go out unattributed —
    # the durable record's missing declared actor must surface.
    request_actor = None if tamper == "missing-actor" else ACTOR
    try:
        rig = pair.launch_pair(args, declared, auto_promote=budget)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        duty_name, standby_name = (
            duty_decl["name"],
            standby_decl["name"],
        )

        # Phase 1 — convergence on the armed pair.
        converged = rig.converge(failures)
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )
        evidence["converged"] = converged["ticks"][-1]

        # Phase 2 — the attributed forward switch: the demote's
        # request boundary on the field owner, the promote's on the
        # tracking standby, both journaled carrying the actor.
        forward = attributed_switch(
            rig,
            duty_url,
            standby_url,
            failures,
            request_actor,
            demote_what=f"the field owner {duty_name}",
            promote_what=f"the tracking standby {standby_name}",
        )
        digest_entries.append({"phase": "switch", "forward": forward})
        evidence["switched_at"] = forward["demote"]["tick"]

        # Phase 3 — the tracking peer's checkpoint-adopted journal
        # shows the same attributed record: a receipted invoke
        # settled on the promoted owner journals on the demoted
        # tracker with the declared actor carried over the
        # checkpoint.
        command = declared_invoke(standby_url, failures)
        _b, duty_entries0 = journal_entries(
            rig.duty_files["journal_file"]
        )
        status, receipt = pair.request(
            f"{standby_url}/command",
            {"command": command, "actor": ACTOR},
        )
        if status != 200 or (
            simulate.receipt_outcome(receipt) != "accepted"
        ):
            failures.append(
                f"the promoted peer's /command answered {status} "
                f"{receipt}, expected an accepted receipt"
            )
            raise Abort
        _tracked, owner = rig.tick(duty_url, standby_url, failures)
        _b, duty_entries = journal_entries(
            rig.duty_files["journal_file"]
        )
        adopted = audit_adopted(
            duty_entries,
            len(duty_entries0),
            command,
            duty_name,
            failures,
        )
        digest_entries.append(
            {
                "phase": "adopted",
                "command": command["invoke"]["command"],
                "receipt": receipt,
                "adopted_actor": (adopted or {}).get("actor"),
            }
        )
        if failures:
            raise Abort

        # Phase 4 — the attributed fail-back switch restoring the
        # launch roles: the promoted peer demotes, the converged
        # duty peer promotes, same durable attribution on both.
        failback = attributed_switch(
            rig,
            standby_url,
            duty_url,
            failures,
            request_actor,
            demote_what=f"the field owner {standby_name}",
            promote_what=f"the tracking peer {duty_name}",
        )
        digest_entries.append(
            {"phase": "switch", "failback": failback}
        )
        evidence["failback_at"] = failback["demote"]["tick"]

        # Phase 5 — the automatic half: severing the field owner's
        # checkpoint serving runs the armed standby's consecutive
        # miss count to the declared budget, whose boundary
        # self-promotes it.
        severance_tick = role_floor(standby_url, failures)
        standby_floor = durable_floor(
            rig.standby_files["journal_file"], standby_name, failures
        )
        pair.stop(rig.duty)
        rig.duty = None
        misses = []
        for miss in range(1, budget):
            pair.scan(standby_url, failures)
            report = pair.get(
                f"{standby_url}/role", "GET /role", failures
            )
            sync = report.get("sync")
            if report.get("role") != "standby" or not (
                isinstance(sync, dict) and "degraded" in sync
            ):
                failures.append(
                    f"miss {miss} left the standby reporting "
                    f"{report} — expected standby under the "
                    "degraded miss run inside the declared budget"
                )
                raise Abort
            misses.append(
                {
                    "miss": miss,
                    "tick": report.get("tick"),
                    "role": report.get("role"),
                }
            )
        pair.scan(standby_url, failures)
        report = pair.get(
            f"{standby_url}/role", "GET /role", failures
        )
        promotion_tick = report.get("tick")
        if report.get("role") != "active" or (
            promotion_tick != severance_tick + budget
        ):
            failures.append(
                "the standby never self-promoted at its declared "
                f"budget — GET /role answers {report}, expected "
                f"active at {severance_tick + budget}"
            )
            raise Abort
        evidence["promoted"] = promotion_tick
        digest_entries.append(
            {
                "phase": "failover",
                "budget": budget,
                "misses": misses,
                "promotion_tick": promotion_tick,
            }
        )
        # The self-promotion's durable record: `origin: "failover"`
        # and no operator actor — never reading as an unattributed
        # request. Under the doctored case the expectation demands
        # the misclassified shape, so the audit proves it fires.
        want_origin = (
            "request" if tamper == "failover-as-request" else "failover"
        )
        want_actor = (
            ACTOR if tamper == "failover-as-request" else None
        )
        _boundaries, standby_entries = journal_entries(
            rig.standby_files["journal_file"]
        )
        gained = role_changes(standby_entries[standby_floor:])
        pairs = [
            (frm, to) for _tick, frm, to, _origin, _actor in gained
        ]
        want = [("standby", "promoting"), ("promoting", "active")]
        if pairs != want:
            failures.append(
                f"the promoted peer's durable journal gained the "
                f"role transitions {gained} across the failover, "
                f"expected {want}"
            )
        else:
            for tick, frm, to, origin, seen in gained:
                if origin != want_origin or seen != want_actor:
                    if tamper == "failover-as-request":
                        failures.append(
                            "the self-promotion's durable record "
                            f"carries {frm}->{to} at tick {tick} "
                            f"with origin={origin!r} actor={seen!r}, "
                            "expected the self-promotion's record "
                            "reading as an operator request"
                        )
                    else:
                        failures.append(
                            "the self-promotion's durable record "
                            f"carries {frm}->{to} at tick {tick} "
                            f"with origin={origin!r} actor={seen!r} "
                            "— the automatic promotion must journal "
                            "origin 'failover' with no operator "
                            "actor, distinguishable from any "
                            "operator request"
                        )
            if failures:
                raise Abort
        digest_entries.append(
            {"phase": "failover_record", "transitions": gained}
        )
        continued = []
        for _ in range(CONTINUE_TICKS):
            snapshot = pair.scan(standby_url, failures)
            continued.append(snapshot["tick"])
        digest_entries.append(
            {"phase": "continued", "ticks": continued}
        )

        # Phase 6 — the restore: the duty peer respawns on its
        # declared listen address tracking the promoted peer —
        # the demoted standby's configured --standby then resolves
        # to it — reconverges, and an attributed switch returns the
        # launch roles.
        duty_addr = duty_url.removeprefix("http://")
        standby_addr = standby_url.removeprefix("http://")
        rig.duty, resumed_url, preamble = pair.spawn_peer(
            args.controller,
            args.model,
            args.dt,
            rig.plant_addr,
            standby_addr,
            rig.duty_files,
            listen=duty_addr,
            pair_token=pair.PAIR_TOKEN,
        )
        if resumed_url is None:
            raise Abort(
                f"the respawned duty controller {duty_name} exited "
                "at startup: "
                f"{'; '.join(preamble) or 'no diagnostic'}"
            )
        resumed = []
        duty_report = None
        for _ in range(TRACK_BOUND):
            _tracked, owner = rig.tick(duty_url, standby_url, failures)
            resumed.append(owner["tick"])
            duty_report = tracking(duty_url, failures)
            if duty_report is not None:
                break
        if duty_report is None:
            failures.append(
                "the respawned duty peer never reconverged "
                "tracking off the promoted standby's checkpoints"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "restore",
                "resume_ticks": resumed,
                "duty_role": duty_report,
            }
        )
        restore = attributed_switch(
            rig,
            standby_url,
            duty_url,
            failures,
            request_actor,
            demote_what=f"the promoted {standby_name}",
            promote_what=f"the reconverged {duty_name}",
        )
        digest_entries.append(
            {"phase": "switch", "restore": restore}
        )
        evidence["restored_at"] = restore["promote"]["tick"]

        # Phase 7 — the durable record: each peer's journal file —
        # boundaries and `seq` order intact, the full attributed
        # role stream — matching its served journal; the state
        # files checkpointed at the run's final tick.
        final_tick = restore["promoted_role"]["tick"]
        expected = {
            duty_name: [
                ("active", "demoting"),
                ("demoting", "standby"),
                ("standby", "promoting"),
                ("promoting", "active"),
                ("standby", "promoting"),
                ("promoting", "active"),
            ],
            standby_name: [
                ("standby", "promoting"),
                ("promoting", "active"),
                ("active", "demoting"),
                ("demoting", "standby"),
                ("standby", "promoting"),
                ("promoting", "active"),
                ("active", "demoting"),
                ("demoting", "standby"),
            ],
        }
        files = {
            duty_name: rig.duty_files,
            standby_name: rig.standby_files,
        }
        urls = {duty_name: duty_url, standby_name: standby_url}
        persisted = {}
        for name in (duty_name, standby_name):
            path = files[name]["journal_file"]
            state_path = files[name]["state_file"]
            boundaries, entries = journal_entries(path)
            seqs = [entry["seq"] for entry in entries]
            if seqs != list(range(1, len(seqs) + 1)):
                failures.append(
                    f"{name}'s journal seqs are not 1..n in order: "
                    f"{seqs}"
                )
            stream = role_changes(entries)
            if [(frm, to) for _t, frm, to, _o, _a in stream] != (
                expected[name]
            ):
                failures.append(
                    f"{name}'s durable journal carries the role "
                    f"transitions {stream}, expected {expected[name]}"
                )
            served = pair.get(
                f"{urls[name]}/journal", "GET /journal", failures
            )
            if role_changes(served) != stream:
                failures.append(
                    f"{name}'s served journal diverges from its "
                    "durable file — the monitor does not answer "
                    "the record it persists"
                )
            record = {
                "boundaries": boundaries,
                "transitions": stream,
                "journal_records": len(boundaries) + len(entries),
            }
            if not os.path.exists(state_path):
                failures.append(
                    f"{name}'s declared state file {state_path} "
                    "does not exist — the --state-file flag was "
                    "not honored"
                )
            else:
                try:
                    with open(state_path) as handle:
                        checkpoint = json.load(handle)
                except (OSError, json.JSONDecodeError) as error:
                    failures.append(
                        f"{name}'s state file does not parse: "
                        f"{error}"
                    )
                    checkpoint = None
                if checkpoint is not None:
                    if checkpoint.get("tick") != final_tick:
                        failures.append(
                            f"{name}'s state file persisted tick "
                            f"{checkpoint.get('tick')} while the "
                            f"run stood at {final_tick}"
                        )
                    record["state_tick"] = checkpoint.get("tick")
            persisted[name] = record
        digest_entries.append(
            {"phase": "record", "persisted": persisted}
        )
        evidence["final_tick"] = final_tick
        evidence["journal_records"] = sum(
            record["journal_records"] for record in persisted.values()
        )
        if failures:
            raise Abort
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
        choices=["missing-actor", "failover-as-request"],
        help="doctor the leg's requests or expectations — the pass "
        "must fail naming the evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = attribution_pass(
            args, args.tamper
        )
    except Abort as abort:
        for line in abort.args:
            eprint(f"attribution: {line}")
        return 1
    for failure in failures:
        eprint(f"attribution: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"attribution: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored "
                "attribution"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"attribution-digest {digest} — tracking by tick "
        f"{evidence['converged']}, attributed switches at ticks "
        f"{evidence['switched_at']}, {evidence['failback_at']}, "
        f"self-promoted at tick {evidence['promoted']} on the "
        "declared miss budget, roles restored at tick "
        f"{evidence['restored_at']}, run continued to tick "
        f"{evidence['final_tick']}, "
        f"{evidence['journal_records']} persisted journal records"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
