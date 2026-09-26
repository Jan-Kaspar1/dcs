#!/usr/bin/env python3
"""The pair contract's shelving-reason leg — decision 72's recorded
shelving-reason form exercised end to end on the deployed consumer
pair (WW-ALM-002, WW-ENG-003), the consumer-boundary mirror of the
qa-rig leg at qa_lane/scenarios/3050_shelving_reason.py.

ci/check.sh runs this script twice against the same declared
deployment as ci/legs/pair.py — `dcs-plant-server` serving the emitted
model and dynamics, plus the two manifest-declared `dcs-controller
--driven --remote` peers, the standby wired at the field owner's
monitor. With the pair converged and tracking, the leg resolves the
emitted model's shelve surface — the managed alarms binding a
writable `shelve` input under a nonzero declared `max_shelve_ticks`,
in kind:name order — and drives:

- the reasoned shelve: a receipted `write_value` on the first
  shelvable alarm's `shelve` point carrying the leg's actor and
  declared reason — the `accepted` submission settling `applied`,
  the settled receipt carrying actor and reason, the journaled
  `command_settled` echoing both beside the `shelved` point_changed
  transition at the applied tick, and the served managed list
  reporting the standing shelve;
- the bounded expiry: the declared `max_shelve_ticks` releasing the
  shelve with the request still standing — the journaled release
  transition landing at applied + bound and pairing no command, the
  automatic expiry attributing to no receipt;
- the mandatory refusal: a reasonless shelve on a
  `requires_reason`-marked point refused at admission with the named
  `reason_required` verdict, journaled as a settled rejection —
  state unchanged;
- the voluntary path: a reasonless shelve on an unmarked point
  settling `applied` with no reason anywhere on its receipt;
- the restore: the standing requests released through the same
  receipted path — the marked point's release itself reasoned —
  the pair's roles unmoved, both peers' adopted receipt logs
  identical, and the served journal answering the durable file's
  record.

On success one `shelving-reason-digest <sha256>` line prints — the
check runs two passes and compares them. Every assertion failure
collects onto stderr prefixed `shelving-reason:` and exits 1 — the
check names it shelving-reason-failed; differing digests name
shelving-reason-nondeterministic.

`--tamper` doctors the leg's own expectation so a doctored
implementation — a settled receipt dropping the reason, or a
`reason_required` refusal bypassed — would pass while the honest
run reports the named failure:

- `expect-reasonless` asserts the reasoned shelve's settled receipt
  carries no reason — the honest receipt's declared reason fails it;
- `expect-applied` asserts the reasonless shelve on the marked
  point settles `applied` — the honest `reason_required` refusal
  fails it.

Both exit 1 like any failure: the check asserts each reports the
named diagnostic rather than passing silently.
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
# The doctored cases: a leg asserting the reasoned shelve's settled
# receipt carries no reason — the receipt dropping the declared
# reason — and a leg asserting the reasonless shelve on the marked
# point settled applied — the mandatory-reason refusal bypassed —
# must each surface the named diagnostic rather than passing
# silently.
LEG = {
    "order": 270,
    "title": "the shelving-reason leg",
    "passes": "shelving-reason",
    "tampers": [
        {
            "name": "expect-reasonless",
            "passed": "a expect-reasonless case passed the shelving-reason leg",
            "missed": "the expect-reasonless case did not report its named diagnostic",
            "evidence": ["the doctored expectation wanted no reason"],
        },
        {
            "name": "expect-applied",
            "passed": "a expect-applied case passed the shelving-reason leg",
            "missed": "the expect-applied case did not report its named diagnostic",
            "evidence": ["expected an applied receipt"],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The bound each driven phase gets to land its declared effect across
# the wiring's one-scan carrier crossings.
SETTLE_BOUND = 16

# The actor every receipted submission declares, and the reason
# strings the marked point's reasoned commands carry — the shelve
# itself and its restore.
ACTOR = "ci-shelving-reason"
REASON = "shelving-reason audit shelve"
RELEASE_REASON = "shelving-reason audit restore"

# The component kinds the emitted model's managed-alarm surface
# declares — the descriptor kinds a `shelve` input port binds on.
MANAGED_KINDS = ("managed-bool-latching-alarm", "managed-latching-alarm")


def shelve_candidates(model):
    """The emitted model's shelve surface: each managed alarm — in
    `<kind>:<id>` order, the same name order the served descriptors
    and the rig leg sort by — binding a writable `shelve` input on a
    declared `In` point and a `shelved` status output, under a
    nonzero declared `max_shelve_ticks`. Each candidate carries its
    request point's `requires_reason` mark — the model-declared
    admission gate the leg exercises — or None when the model
    declares no shelvable managed alarm."""
    points = {point["id"]: point for point in model.get("io_points", [])}
    kinds = {}
    bound = {}
    for component in model.get("components", []):
        if component.get("kind") in MANAGED_KINDS:
            kinds[component["id"]] = component["kind"]
            parameter = component.get("parameters", {}).get(
                "max_shelve_ticks", {}
            )
            bound[component["id"]] = parameter.get("int")
    shelve = {}
    shelved = {}
    for connection in model.get("connections", []):
        port = connection.get("to", {}).get("port", {})
        if port.get("name") == "shelve":
            point = points.get(connection.get("from", {}).get("point"))
            if point is not None:
                shelve[port.get("component")] = point
        port = connection.get("from", {}).get("port", {})
        if port.get("name") == "shelved":
            point = connection.get("to", {}).get("point")
            if point in points:
                shelved[port.get("component")] = point
    candidates = []
    for ident in sorted(
        set(bound) & set(shelve) & set(shelved),
        key=lambda ident: f"{kinds[ident]}:{ident}",
    ):
        request = shelve[ident]
        if (
            not isinstance(bound[ident], int)
            or bound[ident] <= 0
            or request["direction"] != "in"
            or request["value_type"] != "bool"
            or not request.get("writable")
        ):
            continue
        candidates.append(
            {
                "component": ident,
                "shelve": request["id"],
                "shelved": shelved[ident],
                "bound": bound[ident],
                "requires_reason": bool(request.get("requires_reason")),
            }
        )
    return candidates or None


def point_names(model):
    """The point→signal-name map the emitted model's declared signal
    index resolves — the names failure lines carry."""
    return {
        entry["point"]: entry["name"]
        for entry in simulate.declared_signal_index(model)["points"]
    }


def value(snapshot, point):
    """The point's latest sample value — `simulate.snapshot_point`."""
    return simulate.snapshot_point(snapshot, point)


def write_value(point, boolean):
    """A `write_value` command body for the receipted path."""
    return {
        "write_value": {
            "kind": "bool",
            "point": point,
            "value": {"bool": boolean},
        }
    }


def submit(url, command, reason, failures):
    """POST one receipted write to the active's `/command` — the
    leg's actor always, `reason` only when the submission declares
    one — and assert the `accepted` submission. Returns the
    receipt."""
    envelope = {"command": command, "actor": ACTOR}
    if reason is not None:
        envelope["reason"] = reason
    status, receipt = pair.request(f"{url}/command", envelope)
    if status != 200 or simulate.receipt_outcome(receipt) != "accepted":
        failures.append(
            f"the receipted write {command} answered {status} "
            f"{receipt}, expected an accepted receipt"
        )
        raise Abort
    if receipt.get("actor") != ACTOR:
        failures.append(
            f"the accepted receipt carried actor "
            f"{receipt.get('actor')!r}, not the leg's declared {ACTOR}"
        )
        raise Abort
    return receipt


def settled(receipts, command):
    """The adopted receipt log's settled entry for `command` — the
    receipt carrying the terminal outcome — or None."""
    for entry in receipts:
        if (
            entry.get("command") == command
            and simulate.receipt_outcome(entry) == "applied"
        ):
            return entry
    return None


def applied_tick(receipt, failures, what):
    """The applied outcome's scan tick off a settled receipt —
    `what` names the write in the failure line."""
    applied = (receipt.get("outcome") or {}).get("applied") or {}
    tick = applied.get("tick")
    if not isinstance(tick, int):
        failures.append(
            f"the {what} receipt's applied outcome carries no tick: "
            f"{receipt}"
        )
        raise Abort
    return tick


def lifecycle(entries, shelve_point, shelved_point):
    """The leg's lifecycle records out of a journal entry list —
    `(receipts, transitions)` where receipts are `{"seq", "tick",
    "receipt"}` for each `command_settled` on the shelve request
    point and transitions `{"seq", "tick", "from", "to"}` for each
    `point_changed` on the shelved status point, in `seq` order."""
    receipts, transitions = [], []
    for entry in entries:
        event = entry.get("event", {})
        if "command_settled" in event:
            receipt = event["command_settled"].get("receipt", {})
            write = receipt.get("command", {}).get("write_value", {})
            if write.get("point") == shelve_point:
                receipts.append(
                    {
                        "seq": entry.get("seq"),
                        "tick": entry.get("tick"),
                        "receipt": receipt,
                    }
                )
        elif "point_changed" in event:
            change = event["point_changed"]
            if change.get("point") == shelved_point:
                transitions.append(
                    {
                        "seq": entry.get("seq"),
                        "tick": entry.get("tick"),
                        "from": change.get("from"),
                        "to": change.get("to"),
                    }
                )
    return receipts, transitions


def journal_entries(rig):
    """The field owner's durable journal entries — `entry` records
    only, run boundaries excluded."""
    path = rig.duty_files.get("journal_file")
    if path is None or not os.path.exists(path):
        raise Abort(
            "the field owner's declared journal file does not exist "
            "— the --journal-file flag was not honored"
        )
    return [
        record
        for kind, record in pair.journal_records(path)
        if kind == "entry"
    ]


def tick(rig, failures):
    """One driven pair tick — the tracking peer scanned first so its
    pull applies the owner's latest checkpoint, then the field owner —
    asserting the peers' images stay identical. Returns the owner's
    served snapshot."""
    return rig.tick(rig.standby_url, rig.duty_url, failures)[1]


def drive_until(rig, failures, condition, bound=SETTLE_BOUND):
    """Driven pair ticks until `condition(owner_snapshot)` holds —
    returns the satisfying snapshot, or None when `bound` scans pass
    without it landing."""
    for _ in range(bound):
        owner = tick(rig, failures)
        if condition(owner):
            return owner
    return None


def shelving_pass(args, tamper):
    """The shelving-reason run: converge, reasoned shelve, expiry,
    refusal, reasonless settle, release, restore, audit. Returns
    `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "shelving-reason leg has nothing to exercise"
        )
    with open(args.model) as handle:
        model = json.load(handle)
    candidates = shelve_candidates(model)
    if candidates is None:
        raise Abort(
            "the emitted model declares no shelvable managed alarm — "
            "the leg has nothing to exercise"
        )
    target = candidates[0]
    marked = next(
        (entry for entry in candidates if entry["requires_reason"]), None
    )
    unmarked = next(
        (entry for entry in candidates if not entry["requires_reason"]),
        None,
    )
    if marked is None:
        raise Abort(
            "the emitted model marks no shelve point requires_reason "
            "— the reason_required refusal has nothing to refuse"
        )
    if unmarked is None:
        raise Abort(
            "every shelvable managed alarm declares requires_reason — "
            "the model offers no unmarked point for the reasonless "
            "shelve"
        )
    names = point_names(model)
    true, false = {"bool": True}, {"bool": False}
    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url

        # Phase 1 — convergence: the pair leg's driven-tick loop, the
        # tracking peer scanned first so each pull applies the owner's
        # latest checkpoint and the peers rest at the same tick with
        # identical images.
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
        owner = converged["owner"]
        for candidate in (target, unmarked):
            for key in ("shelve", "shelved"):
                if value(owner, candidate[key]) != false:
                    failures.append(
                        "the converged pair is not quiet — "
                        f"{names[candidate[key]]} reads "
                        f"{value(owner, candidate[key])} before the "
                        "leg drives"
                    )
        if failures:
            raise Abort

        # The served index must carry the mark — the consumer-facing
        # surface the mandatory declaration lands on.
        served = pair.get(f"{duty_url}/signals", "GET /signals", failures)
        served_points = {
            entry["point"]: entry for entry in served.get("points", [])
        }
        if served_points.get(marked["shelve"], {}).get(
            "requires_reason"
        ) is not True:
            failures.append(
                f"the served index does not mark "
                f"{names[marked['shelve']]} requires_reason — the "
                "consumer surface the gate declares on is absent"
            )
        if served_points.get(unmarked["shelve"], {}).get(
            "requires_reason"
        ):
            failures.append(
                f"the served index marks {names[unmarked['shelve']]} "
                "requires_reason though the model leaves it unmarked"
            )
        if failures:
            raise Abort

        # Phase 2 — the reasoned shelve: the first shelvable alarm's
        # writable `shelve` point takes the receipted request under
        # the leg's declared actor and reason; `shelved` reports
        # while the request stands — the served managed list's
        # standing shelve.
        shelve_write = write_value(target["shelve"], True)
        submit(duty_url, shelve_write, REASON, failures)
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: value(snapshot, target["shelved"]) == true,
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                "the reasoned shelve never reported shelved — "
                f"{names[target['shelved']]} reads "
                f"{value(owner, target['shelved'])}, the request "
                f"{names[target['shelve']]} "
                f"{value(owner, target['shelve'])}"
            )
            raise Abort
        evidence["shelved_at"] = owner["tick"]
        receipts = pair.get(f"{duty_url}/receipts", "GET /receipts", failures)
        receipt = settled(receipts, shelve_write)
        if receipt is None:
            failures.append(
                "the reasoned shelve never settled applied into the "
                "adopted receipt log"
            )
            raise Abort
        bad = []
        if receipt.get("actor") != ACTOR:
            bad.append(
                f"the settled receipt lost the declared actor: "
                f"{receipt.get('actor')!r}"
            )
        if tamper == "expect-reasonless":
            if receipt.get("reason") is not None:
                bad.append(
                    f"the settled receipt carries reason "
                    f"{receipt.get('reason')!r} — the doctored "
                    "expectation wanted no reason"
                )
        elif receipt.get("reason") != REASON:
            bad.append(
                f"the settled receipt lost the declared reason: "
                f"{receipt.get('reason')!r}"
            )
        if bad:
            failures.extend(bad)
            raise Abort
        tick_applied = applied_tick(receipt, failures, "reasoned shelve")
        evidence["applied_at"] = tick_applied

        # Phase 3 — the durable record: the field owner's journal
        # carries the attributed receipt beside the lifecycle
        # transition — the receipt's seq precedes the shelved rise and
        # the pair shares the applied tick, the attribution the
        # managed list renders; the served journal answers the same
        # record.
        entries = journal_entries(rig)
        file_receipts, transitions = lifecycle(
            entries, target["shelve"], target["shelved"]
        )
        rise = next(
            (entry for entry in transitions if entry.get("to") == true),
            None,
        )
        reasoned = [
            entry
            for entry in file_receipts
            if entry["receipt"].get("command") == shelve_write
            and "applied" in (entry["receipt"].get("outcome") or {})
        ]
        bad = []
        if not reasoned:
            bad.append(
                "no journaled applied receipt names the reasoned "
                "shelve write"
            )
        else:
            journaled = reasoned[-1]["receipt"]
            if journaled.get("actor") != ACTOR:
                bad.append(
                    "the journaled receipt lost the declared actor: "
                    f"{journaled.get('actor')!r}"
                )
            if journaled.get("reason") != REASON:
                bad.append(
                    "the journaled receipt lost the declared reason: "
                    f"{journaled.get('reason')!r}"
                )
            if rise is not None and not (
                reasoned[-1]["seq"] < rise["seq"]
            ):
                bad.append(
                    f"the journaled receipt (seq "
                    f"{reasoned[-1]['seq']}) does not precede the "
                    f"shelved transition (seq {rise['seq']})"
                )
        if rise is None:
            bad.append(
                "the durable journal carries no shelved transition "
                "the reasoned receipt pairs"
            )
        elif rise["tick"] != tick_applied:
            bad.append(
                f"the shelved rise journaled at tick {rise['tick']} — "
                f"the applied receipt settled at {tick_applied}, so "
                "the transition pairs no reasoned receipt"
            )
        served = pair.get(f"{duty_url}/journal", "GET /journal", failures)
        served_lifecycle = lifecycle(
            served, target["shelve"], target["shelved"]
        )
        if served_lifecycle != (file_receipts, transitions):
            bad.append(
                "the served journal's shelve lifecycle diverges from "
                "the durable file's — the monitor does not answer "
                "the record it persists"
            )
        if bad:
            failures.extend(bad)
            raise Abort
        digest_entries.append(
            {
                "phase": "shelve",
                "component": target["component"],
                "bound": target["bound"],
                "applied_at": tick_applied,
                "receipt_seq": reasoned[-1]["seq"],
                "rise_seq": rise["seq"],
            }
        )

        # Phase 4 — the bounded expiry: the declared bound releases
        # the shelve with the request still standing — the journaled
        # drop landing at applied + max_shelve_ticks and pairing no
        # command, the automatic expiry attributing to no receipt.
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: value(snapshot, target["shelved"]) == false,
            bound=target["bound"] + SETTLE_BOUND,
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                "the bounded shelve never released — "
                f"{names[target['shelved']]} still reads true "
                f"{target['bound'] + SETTLE_BOUND} scans after the "
                f"request landed, the declared bound "
                f"{target['bound']}"
            )
            raise Abort
        if value(owner, target["shelve"]) != true:
            failures.append(
                "the shelve released with its request withdrawn — "
                f"{names[target['shelve']]} reads "
                f"{value(owner, target['shelve'])}, not the bound's "
                "own auto-release"
            )
            raise Abort
        entries = journal_entries(rig)
        file_receipts, transitions = lifecycle(
            entries, target["shelve"], target["shelved"]
        )
        drop = next(
            (
                entry
                for entry in transitions
                if entry.get("to") == false and entry["tick"] > tick_applied
            ),
            None,
        )
        if drop is None:
            failures.append(
                "the durable journal never recorded the shelve's "
                f"expiry — the transitions read {transitions}"
            )
            raise Abort
        if drop["tick"] != tick_applied + target["bound"]:
            failures.append(
                f"the expiry drop journaled at tick {drop['tick']} — "
                f"the declared bound lands it at "
                f"{tick_applied + target['bound']}"
            )
            raise Abort
        paired = [
            entry
            for entry in file_receipts
            if ((entry["receipt"].get("outcome") or {}).get("applied") or {})
            .get("tick")
            == drop["tick"]
        ]
        if paired:
            failures.append(
                f"a settled receipt pairs the expiry tick "
                f"{drop['tick']} — the automatic release must "
                f"attribute to no command: {paired[0]['receipt']}"
            )
            raise Abort
        evidence["released_at"] = drop["tick"]
        digest_entries.append(
            {
                "phase": "expiry",
                "applied_at": tick_applied,
                "released_at": drop["tick"],
                "drop_seq": drop["seq"],
            }
        )

        # Phase 5 — the mandatory refusal: a reasonless shelve on the
        # marked point answers the named `reason_required` admission
        # verdict on the attributed receipt — the journaled
        # `command_settled` rejection — and no state changes.
        status, refusal = pair.request(
            f"{duty_url}/command",
            {
                "command": write_value(marked["shelve"], True),
                "actor": ACTOR,
            },
        )
        if tamper == "expect-applied":
            if (
                status != 200
                or simulate.receipt_outcome(refusal) != "applied"
            ):
                failures.append(
                    f"the reasonless shelve on the reason-mandated "
                    f"point answered {status} {refusal} — expected "
                    "an applied receipt"
                )
                raise Abort
        else:
            if (
                status != 200
                or simulate.receipt_outcome(refusal) != "reason_required"
            ):
                failures.append(
                    f"the reasonless shelve on the reason-mandated "
                    f"point answered {status} {refusal}, expected "
                    "the named reason_required refusal"
                )
                raise Abort
            if refusal.get("actor") != ACTOR:
                failures.append(
                    f"the refusal carried actor "
                    f"{refusal.get('actor')!r}, not the leg's "
                    f"declared {ACTOR}"
                )
                raise Abort
        owner = tick(rig, failures)
        entries = journal_entries(rig)
        refused = [
            entry
            for entry in lifecycle(
                entries, marked["shelve"], marked["shelved"]
            )[0]
            if entry["receipt"].get("command")
            == write_value(marked["shelve"], True)
            and simulate.receipt_outcome(entry["receipt"])
            == "reason_required"
        ]
        if tamper != "expect-applied" and not refused:
            failures.append(
                "the durable journal carries no reason_required "
                "settlement for the refused shelve — the named "
                "rejection never journaled"
            )
            raise Abort
        evidence["refused_at"] = owner["tick"]

        # Phase 6 — the voluntary path: a reasonless shelve on the
        # unmarked point settles `applied` with no reason on its
        # receipt — the pre-contract path — then releases the same
        # way.
        plain_write = write_value(unmarked["shelve"], True)
        submit(duty_url, plain_write, None, failures)
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: value(snapshot, unmarked["shelved"]) == true,
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                "the reasonless shelve on the unmarked point never "
                f"reported shelved — {names[unmarked['shelved']]} "
                f"reads {value(owner, unmarked['shelved'])}"
            )
            raise Abort
        receipts = pair.get(f"{duty_url}/receipts", "GET /receipts", failures)
        receipt = settled(receipts, plain_write)
        if receipt is None:
            failures.append(
                "the reasonless shelve on the unmarked point never "
                "settled applied into the adopted receipt log — the "
                "declaration mandates no reason, so the path is the "
                "pre-contract one"
            )
            raise Abort
        if receipt.get("reason") is not None:
            failures.append(
                "the reasonless shelve's receipt carries a reason "
                f"the submission never declared: "
                f"{receipt.get('reason')!r}"
            )
            raise Abort
        evidence["plain_shelved_at"] = owner["tick"]
        plain_release = write_value(unmarked["shelve"], False)
        submit(duty_url, plain_release, None, failures)
        owner = tick(rig, failures)
        if value(owner, unmarked["shelve"]) != false:
            failures.append(
                "the unmarked shelve's release left the request "
                f"standing — {names[unmarked['shelve']]} reads "
                f"{value(owner, unmarked['shelve'])}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "reasonless",
                "component": unmarked["component"],
                "shelved_at": evidence["plain_shelved_at"],
            }
        )

        # Phase 7 — the restore: the standing request on the marked
        # target releases through the same receipted path — the
        # release itself reasoned, the marked point's admission gate
        # demanding one — leaving every shelve request down.
        release_write = write_value(target["shelve"], False)
        submit(duty_url, release_write, RELEASE_REASON, failures)
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: value(snapshot, target["shelve"]) == false,
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                "the reasoned release never dropped the request — "
                f"{names[target['shelve']]} reads "
                f"{value(owner, target['shelve'])}"
            )
            raise Abort
        receipts = pair.get(f"{duty_url}/receipts", "GET /receipts", failures)
        receipt = settled(receipts, release_write)
        if receipt is None or receipt.get("reason") != RELEASE_REASON:
            failures.append(
                "the reasoned release never settled applied carrying "
                f"its declared reason — the receipt reads {receipt}"
            )
            raise Abort
        evidence["restored_at"] = owner["tick"]

        # Phase 8 — the restoration and the record: the pair's launch
        # roles never moved, every driven request reads restored, the
        # peers' adopted receipt logs are identical, and the durable
        # journal's shelve lifecycle answers the served record — seqs
        # contiguous across the run.
        standby_role = pair.get(f"{standby_url}/role", "GET /role", failures)
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        sync = standby_role.get("sync")
        if standby_role.get("role") != "standby" or not (
            isinstance(sync, dict) and "tracking" in sync
        ):
            failures.append(
                "the standby's role moved through the leg — GET "
                f"/role answers {standby_role}"
            )
        if duty_role.get("role") != "active":
            failures.append(
                "the field owner's role moved through the leg — GET "
                f"/role answers {duty_role}"
            )
        for candidate in (target, unmarked):
            if value(owner, candidate["shelve"]) != false:
                failures.append(
                    "the leg left a shelve request standing — "
                    f"{names[candidate['shelve']]} reads "
                    f"{value(owner, candidate['shelve'])} at restore"
                )
            if value(owner, candidate["shelved"]) != false:
                failures.append(
                    "the leg left a shelve standing — "
                    f"{names[candidate['shelved']]} reads "
                    f"{value(owner, candidate['shelved'])} at restore"
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
                "trail is not identical on both peers"
            )
            raise Abort
        entries = journal_entries(rig)
        seqs = [entry["seq"] for entry in entries]
        if seqs != list(range(1, len(seqs) + 1)):
            failures.append(
                f"the field owner's journal seqs are not 1..n in "
                f"order: {seqs}"
            )
            raise Abort
        served = pair.get(f"{duty_url}/journal", "GET /journal", failures)
        audited = [
            (entries, target["shelve"], target["shelved"]),
            (entries, unmarked["shelve"], unmarked["shelved"]),
        ]
        for records, shelve_point, shelved_point in audited:
            if lifecycle(records, shelve_point, shelved_point) != lifecycle(
                served, shelve_point, shelved_point
            ):
                failures.append(
                    "the served journal's shelve lifecycle diverges "
                    "from the durable file's — the monitor does not "
                    "answer the record it persists"
                )
                raise Abort
        digest_entries.append(
            {
                "phase": "audit",
                "target": lifecycle(
                    entries, target["shelve"], target["shelved"]
                ),
                "unmarked": lifecycle(
                    entries, unmarked["shelve"], unmarked["shelved"]
                ),
            }
        )
        evidence["entries"] = len(entries)
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
        choices=["expect-reasonless", "expect-applied"],
        help="doctor the leg's own expectation — the pass must fail "
        "naming the evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = shelving_pass(
            args, args.tamper
        )
    except Abort as abort:
        for line in abort.args:
            eprint(f"shelving-reason: {line}")
        return 1
    for failure in failures:
        eprint(f"shelving-reason: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"shelving-reason: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored "
                "expectation"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"shelving-reason-digest {digest} — tracking by tick "
        f"{evidence['converged']}, shelved at tick "
        f"{evidence['shelved_at']} applied at tick "
        f"{evidence['applied_at']}, expired at tick "
        f"{evidence['released_at']}, refused at tick "
        f"{evidence['refused_at']}, restored at tick "
        f"{evidence['restored_at']}, {evidence['entries']} journal "
        "entries"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
