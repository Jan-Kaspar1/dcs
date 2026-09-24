#!/usr/bin/env python3
"""The pair contract's per-pump out-of-service leg — the
consumer-side proof that a receipted maintenance inhibit on the duty
pump's declared `oos` point excludes it from the group's availability
and hands `duty` to the sibling on the deployed redundant pair
(WW-ENG-003, WW-OPS-001, WW-ALM-002).

ci/check.sh runs this script twice against the same declared
deployment as ci/legs/pair.py — `dcs-plant-server` serving the emitted
model and dynamics, plus the two manifest-declared `dcs-controller
--driven --remote` peers, the standby wired at the field owner's
monitor. The leg mirrors the rig-side `scenario_pump_out_of_service`
on the customer-owned pair: each pump's writable `oos` point is
journaled and receipted, and the emitted model wires its inversion
into the pump's in-service availability leg (`oos-ok` →
`oos-ok-avail-in`), the demand guard (`oos-ok-guard-in`), and the
managed per-pump alarms' declared `oos`/`suppress` inputs — so one
receipted write both excludes the machine from duty and manages its
alarm surface. With the pair converged and tracking at an idle
assigned-duty baseline — `duty` naming the pump whose `oos` the leg
drives, so the handover's only cause is the exclusion — the leg:

- submits the attributed receipted `write_value` hold on the duty
  pump's `oos` point through the active's `POST /command`, then
  asserts the exclusion lands inside the declared wiring-depth bound:
  the `oos-ok` cone — the availability leg and the demand guard
  alike — falls, the aggregated `avail` and its delivered `avail-in`
  copy drop, `duty` hands to the sibling, and `staged` reports only
  the available count;
- asserts each managed per-pump alarm reports the states its declared
  lifecycle bindings select — the fault alarm `out_of_service` and
  `suppressed` through its declared `oos`/`suppress` inputs, the
  unbound thermal/moisture kinds and the sibling's whole set
  untouched — the declared precedence read off the served
  descriptors, never assumed;
- watches the sibling serve the next demand through the driven ticks
  — the held pump's `cmd` never re-asserting once its delivered
  availability dropped, `duty` never naming it, `staged` never
  exceeding the available count — then injects a non-Good on the held
  pump's run contact through the plant protocol's unfenced surface
  mid-OOS: the proven fault asserts `alarm` as process truth while
  suppression withholds the `unacknowledged` annunciation;
- writes the point false through the receipted path: the manual
  return reopens the in-service leg, `avail` rejoins, and
  suppression's release re-annunciates the outlasted trip as a fresh
  `unacknowledged` — the declared contract's honest reporting, never
  a swallowed alarm — before the cleared contact returns the
  condition with the latch standing for the receipted `ack`;
- proves rotation eligibility returned: the next completed cycle's
  alternate-each-cycle rotation hands `duty` back to the returned
  pump and the standing demand stages it;
- audits the field owner's durable journal file: the attributed
  settlements and each declared-journaled point's transitions
  carried in `seq` order — the hold and release, the avail drop and
  rejoin, the suppressed trip, the re-annunciation, and the ack —
  with the tick-domain ordering the declared wiring bound measures,
  the served `GET /journal` answering the same record, the peers'
  adopted receipt logs one identical log, and the pair's controller
  roles unmoved throughout.

Usage:

    oos.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `oos-digest <sha256>` line prints — the check runs two
passes and compares them (`oos-nondeterministic`). A contract
violation reports `oos: …` lines on stderr and exits 1 — the check's
`oos-failed`. `--tamper` doctors the leg's own expectation so a
doctored implementation would pass while the honest run reports the
named failure:

- `keeps-duty` asserts the held-out pump keeps `duty` — the honest
  handover to the sibling fails it;
- `managed-silent` asserts the managed alarms never report their
  declared `out_of_service`/`suppressed` states — the honest reports
  fail it.

Both exit 1 like any failure: the check asserts each reports the
named diagnostic rather than passing silently.
"""

import argparse
import hashlib
import json
import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "legs")
)

import pair
import simulate


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The driven ticks each phase's declared effect gets to land: the
# settle bound on each written cause's carrier hops, and the bound on
# each served demand cycle's window — the emitted dynamics lift the
# level through the chain's crossings unopposed well inside it.
# `OOS_BOUND_TICKS` is the declared wiring depth the handover itself
# must land inside: the `oos` write's journaled transition tick to
# the aggregate's drop, the carrier hop to the group's `avail-in`
# read, the reassignment's `duty` report. The actor the leg's
# receipted submissions declare — the attribution every settled
# receipt must carry.
SETTLE_BOUND = 16
CYCLE_BOUND = 48
# The returned pump rejoins on the first demand falling edge after
# its availability and fault clearance both stand — the exclusion or
# a still-standing fault skips it — then the next onset stages it:
# up to two full demand cycles of the emitted dynamics.
REJOIN_BOUND = 96
OOS_BOUND_TICKS = 8
ACTOR = "ci-oos"

# The injected non-Good the fault surface carries — the same quality
# the lifecycle and carryover legs' suppressed trips use.
BAD_QUALITY = {"bad": "device_fault"}

# The signal names resolving the leg's point ids out of the emitted
# model — the same names the signal index serves, so the leg exercises
# the declared seam, never a hard-coded id.
SIGNALS = {
    "demand_in": "demand-in",
    "duty": "duty",
    "staged": "staged",
    "none_available": "none-available",
}
for _index in (1, 2):
    _tag = f"p{100 + _index}"
    for _suffix, _key in (
        ("oos", "oos"),
        ("oos-ok", "oos_ok"),
        ("oos-ok-avail-in", "oos_ok_avail"),
        ("oos-ok-guard-in", "oos_ok_guard"),
        ("avail", "avail"),
        ("avail-in", "avail_in"),
        ("cmd", "cmd"),
        ("run", "run"),
        ("fault", "fault"),
        ("fault-alarm-in", "fault_in"),
        ("fault-sup", "fault_sup"),
        ("fault-sup-in", "fault_sup_in"),
    ):
        SIGNALS[f"{_key}_{_index}"] = f"{_tag}-{_suffix}"
    for _kind in ("fault", "thermal", "moisture"):
        for _flag in (
            "ack",
            "alarm",
            "unacknowledged",
            "shelved",
            "suppressed",
            "out-of-service",
        ):
            SIGNALS[f"{_kind}_{_index}_{_flag.replace('-', '_')}"] = (
                f"{_tag}-{_kind}-{_flag}"
            )

FLAG_KEYS = [
    f"{kind}_{index}_{flag}"
    for index in (1, 2)
    for kind in ("fault", "thermal", "moisture")
    for flag in (
        "alarm",
        "unacknowledged",
        "shelved",
        "suppressed",
        "out_of_service",
    )
]


def signal_points(model):
    """The `{key: point id}` map the emitted model's signal index
    declares for the leg's driven and reported points — None when the
    model declares no such seam. A signal's `source` is the point it
    names; the lowest-signal-id-wins rule the served index applies.
    The receipted `oos` seams must be declared writable *and*
    journaled `In` points — the writable flag admits the receipted
    write, the journaled flag puts the transition on the durable
    record the audit reads; the fault alarms' `ack` inputs must be
    writable for the latch's receipted clear."""
    by_name = {}
    for signal in model.get("signals", []):
        current = by_name.get(signal["name"])
        if current is None or signal["id"] < current[0]:
            by_name[signal["name"]] = (signal["id"], signal["source"])
    points = {}
    for key, signal_name in SIGNALS.items():
        entry = by_name.get(signal_name)
        points[key] = entry[1] if entry else None
    if any(point is None for point in points.values()):
        return None
    declared = {point["id"]: point for point in model.get("io_points", [])}
    for index in (1, 2):
        oos = declared.get(points[f"oos_{index}"], {})
        if not (
            oos.get("writable")
            and oos.get("journaled")
            and oos.get("direction") == "in"
        ):
            return None
        ack = declared.get(points[f"fault_{index}_ack"], {})
        if not ack.get("writable"):
            return None
    return points


def managed_bindings(snapshot, points):
    """The per-pump managed alarms' declared lifecycle bindings —
    `{(index, kind): {port: bound point}}` — resolved from the served
    snapshot's descriptors so the leg reads each kind's declared
    `oos`/`suppress`/`shelve` precedence rather than assuming the
    wiring. Keyed by the point each component's `alarm` output binds —
    the signal the leg already resolved. None when the served
    descriptors bind no managed alarm to a declared alarm point."""
    alarms = {
        points[f"{kind}_{index}_alarm"]: (index, kind)
        for index in (1, 2)
        for kind in ("fault", "thermal", "moisture")
    }
    managed = {}
    for entry in (snapshot or {}).get("descriptors") or []:
        if entry.get("kind") not in (
            "managed-bool-latching-alarm",
            "managed-latching-alarm",
        ):
            continue
        ports = {
            port.get("name"): port.get("point")
            for port in entry.get("ports") or []
        }
        where = alarms.get(ports.get("alarm"))
        if where is not None:
            managed[where] = ports
    if len(managed) != len(alarms):
        return None
    return managed


def value(snapshot, point):
    """The point's latest sample value — `simulate.snapshot_point`."""
    return simulate.snapshot_point(snapshot, point)


def bool_at(snapshot, point):
    """The point's Bool sample value, or None."""
    sample = value(snapshot, point)
    return sample.get("bool") if isinstance(sample, dict) else None


def int_at(snapshot, point):
    """The point's Int sample value, or None."""
    sample = value(snapshot, point)
    return sample.get("int") if isinstance(sample, dict) else None


def write_value(point, boolean):
    """A `write_value` command body for the receipted path."""
    return {
        "write_value": {
            "kind": "bool",
            "point": point,
            "value": {"bool": boolean},
        }
    }


def submit(url, command, failures):
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


def settled_receipt(receipts, command):
    """The adopted receipt log's entry for `command` settled applied,
    or None."""
    return next(
        (
            entry
            for entry in receipts
            if entry.get("command") == command
            and simulate.receipt_outcome(entry) == "applied"
        ),
        None,
    )


def journal_events(entries):
    """The leg's audit stream out of a journal entry list —
    `("settled", point, value, outcome, actor)` for each command
    receipt, `("changed", point, to)` for each journaled value
    transition, and `("quality", point, to)` for each quality
    transition — in `seq` order."""
    events = []
    for entry in entries:
        event = entry.get("event", {})
        if "command_settled" in event:
            receipt = event["command_settled"].get("receipt", {})
            write = receipt.get("command", {}).get("write_value", {})
            events.append(
                (
                    "settled",
                    write.get("point"),
                    write.get("value"),
                    simulate.receipt_outcome(receipt),
                    receipt.get("actor"),
                )
            )
        elif "point_changed" in event:
            change = event["point_changed"]
            events.append(("changed", change.get("point"), change.get("to")))
        elif "quality_changed" in event:
            change = event["quality_changed"]
            events.append(("quality", change.get("point"), change.get("to")))
    return events


def transitions(entries, point, anchor=0):
    """The `(tick, to)` stream one journaled point's `point_changed`
    entries record at or after `anchor`, in `seq` order — the anchor
    drops the cold-start baselines the journal records at run start,
    so an initial quiet value is never mistaken for the leg's
    release."""
    return [
        (entry["tick"], change.get("to"))
        for entry in entries
        if entry["tick"] >= anchor
        for change in [entry.get("event", {}).get("point_changed", {})]
        if change.get("point") == point
    ]


def nth(transition_list, landed, n=1):
    """The tick of the nth transition to `landed`, or None."""
    hits = [tick for tick, to in transition_list if to == landed]
    return hits[n - 1] if len(hits) >= n else None


def subsequence(wanted, got):
    """Whether `wanted`'s values land in `got` in order — the
    journaled per-point transition ordering check."""
    it = iter(got)
    return all(any(item == want for item in it) for want in wanted)


def oos_pass(args, tamper):
    """The out-of-service run: converge, baseline, hold, exclude,
    manage, serve, trip, return, clear, ack, rejoin, audit. Returns
    `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "out-of-service leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    with open(args.model) as handle:
        model = json.load(handle)
    points = signal_points(model)
    if points is None:
        raise Abort(
            "the emitted model declares no writable journaled "
            "per-pump out-of-service seam — the leg has nothing to "
            "exercise"
        )
    true, false = {"bool": True}, {"bool": False}
    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        plant_io = rig.plant_io

        def tick():
            """One driven pair tick — tracking peer first, identical
            images asserted — returns the owner's served snapshot."""
            return rig.tick(standby_url, duty_url, failures)[1]

        def drive_until(condition, bound=SETTLE_BOUND, watch=None):
            """Driven pair ticks until `condition(owner_snapshot)`
            holds — the satisfying snapshot, or None when `bound`
            scans pass without it landing. `watch`, when given, sees
            each tick's snapshot — the leg's opportunistic OOS-window
            invariant checks."""
            for _ in range(bound):
                owner = tick()
                if watch is not None:
                    watch(owner)
                if condition(owner):
                    return owner
            return None

        # Phase 1 — convergence: the tracking peer scanned first so
        # each pull applies the owner's latest checkpoint, the peers
        # resting at the same tick with identical images.
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

        # The managed bindings the served descriptors declare — each
        # per-pump alarm's lifecycle inputs read off the deployment's
        # own contract, so an unbound kind is never expected to report
        # and a bound kind is expected to report exactly its driver's
        # delivered level.
        managed = managed_bindings(converged["owner"], points)
        if managed is None:
            failures.append(
                "the served descriptors bind no managed alarm set to "
                "the declared alarm points — the leg has nothing to "
                "exercise"
            )
            raise Abort

        # Phase 2 — the baseline: the pair settled idle with the
        # group holding an assigned duty — `demand` and `staged`
        # quiet, `duty` naming the pump whose `oos` the leg drives,
        # both pumps available and uninhibited, every managed flag
        # quiet — so the handover's only cause is the exclusion.
        def settled_idle(snapshot):
            if int_at(snapshot, points["demand_in"]) != 0:
                return None
            if int_at(snapshot, points["staged"]) != 0:
                return None
            if int_at(snapshot, points["duty"]) not in (1, 2):
                return None
            if bool_at(snapshot, points["none_available"]) is not False:
                return None
            for index in (1, 2):
                if (
                    bool_at(snapshot, points[f"avail_{index}"]) is not True
                    or bool_at(snapshot, points[f"oos_{index}"]) is not False
                    or bool_at(snapshot, points[f"cmd_{index}"]) is not False
                    or bool_at(snapshot, points[f"fault_{index}"]) is not False
                ):
                    return None
            for key in FLAG_KEYS:
                if bool_at(snapshot, points[key]) is not False:
                    return None
            return snapshot

        owner = drive_until(settled_idle, bound=CYCLE_BOUND)
        if owner is None:
            owner = tick()
            failures.append(
                "the pair never settled to the idle assigned-duty "
                "baseline — demand reads "
                f"{value(owner, points['demand_in'])}, staged "
                f"{value(owner, points['staged'])}, duty "
                f"{value(owner, points['duty'])}"
            )
            raise Abort
        held = int_at(owner, points["duty"])
        sibling = 3 - held
        evidence["baseline_at"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "baseline",
                "tick": owner["tick"],
                "held": held,
                "sibling": sibling,
            }
        )

        # Phase 3 — the receipted hold: the attributed `oos` write
        # settles `applied`, the in-service cone falls through the
        # availability leg and the demand guard alike, the aggregated
        # `avail` and its delivered copy drop, and `duty` hands to the
        # sibling — the exclusion's only cause on an idle pair.
        oos_write = write_value(points[f"oos_{held}"], True)
        submit(duty_url, oos_write, failures)
        breaches = []
        oos_window = [True]
        enforced = [False]

        def watch(snapshot):
            """The OOS-window invariants, enforced once the exclusion
            has fully landed — the propagation scans legitimately show
            the in-flight wiring: after `excluded` reports, the held
            pump must never be commanded or named duty again while the
            hold stands, and `staged` never exceeds the available
            count; `none_available` never reports with the sibling
            available."""
            if not oos_window[0]:
                return
            tick_no = snapshot["tick"]
            if enforced[0]:
                if bool_at(snapshot, points[f"cmd_{held}"]) is True:
                    breaches.append(
                        "the held pump's command re-asserted at tick "
                        f"{tick_no} with its exclusion landed"
                    )
                if int_at(snapshot, points["duty"]) == held:
                    breaches.append(
                        f"duty named the held pump at tick {tick_no} "
                        "with its exclusion landed"
                    )
                staged = int_at(snapshot, points["staged"])
                if isinstance(staged, int) and staged > 1:
                    breaches.append(
                        f"staged reports {staged} at tick {tick_no} — "
                        "past the available count while the hold stood"
                    )
            if bool_at(snapshot, points["none_available"]) is True:
                breaches.append(
                    f"none_available reported at tick {tick_no} "
                    "with the sibling still available"
                )

        def excluded(snapshot):
            if bool_at(snapshot, points[f"oos_{held}"]) is not True:
                return None
            for key in (
                f"oos_ok_{held}",
                f"oos_ok_avail_{held}",
                f"oos_ok_guard_{held}",
                f"avail_{held}",
                f"avail_in_{held}",
            ):
                if bool_at(snapshot, points[key]) is not False:
                    return None
            want_duty = held if tamper == "keeps-duty" else sibling
            if (
                bool_at(snapshot, points[f"avail_{sibling}"]) is not True
                or int_at(snapshot, points["duty"]) != want_duty
                or bool_at(snapshot, points[f"cmd_{held}"]) is not False
                or bool_at(snapshot, points["none_available"]) is not False
            ):
                return None
            enforced[0] = True
            return snapshot

        owner = drive_until(excluded, bound=SETTLE_BOUND, watch=watch)
        if owner is None:
            owner = tick()
            if tamper == "keeps-duty":
                failures.append(
                    f"duty reads {int_at(owner, points['duty'])} — "
                    "the doctored leg expected the held-out pump to "
                    "keep duty, the honest run handed it to the "
                    "sibling"
                )
            else:
                failures.append(
                    "the held pump's exclusion never landed — the "
                    "in-service cone, the avail drop, or the duty "
                    "handover missing: oos reads "
                    f"{value(owner, points[f'oos_{held}'])}, avail "
                    f"{value(owner, points[f'avail_{held}'])}, duty "
                    f"{value(owner, points['duty'])}"
                )
            raise Abort
        evidence["excluded_at"] = owner["tick"]
        hold_receipt = settled_receipt(
            pair.get(f"{duty_url}/receipts", "GET /receipts", failures),
            oos_write,
        )
        if hold_receipt is None:
            failures.append(
                "the out-of-service write never settled applied into "
                "the adopted receipt log"
            )
            raise Abort
        applied = hold_receipt["outcome"]["applied"]["tick"]
        if evidence["excluded_at"] - applied > OOS_BOUND_TICKS:
            failures.append(
                f"the exclusion landed at tick "
                f"{evidence['excluded_at']} — "
                f"{evidence['excluded_at'] - applied} ticks after the "
                f"write's apply tick {applied}, outside the declared "
                f"wiring bound {OOS_BOUND_TICKS}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "excluded",
                "tick": evidence["excluded_at"],
                "applied": hold_receipt["outcome"]["applied"]["tick"],
                "held": held,
                "duty": int_at(owner, points["duty"]),
            }
        )

        # Phase 4 — the managed surface: each per-pump alarm reports
        # the managed states its declared lifecycle bindings select —
        # each bound input's delivered level read in the same
        # snapshot, `False` for an input the kind never declared.
        def managed_want(index, kind, snapshot):
            ports = managed[(index, kind)]

            def bound(port):
                point = ports.get(port)
                return point is not None and bool_at(snapshot, point) is True

            return {
                "shelved": bound("shelve"),
                "suppressed": bound("suppress"),
                "out_of_service": bound("oos"),
            }

        def managed_match(snapshot):
            for index in (1, 2):
                for kind in ("fault", "thermal", "moisture"):
                    for flag, want in managed_want(
                        index, kind, snapshot
                    ).items():
                        if tamper == "managed-silent":
                            want = False
                        if (
                            bool_at(
                                snapshot,
                                points[f"{kind}_{index}_{flag}"],
                            )
                            is not want
                        ):
                            return None
            return snapshot

        owner = drive_until(
            managed_match, bound=SETTLE_BOUND, watch=watch
        )
        if owner is None:
            owner = tick()
            if tamper == "managed-silent":
                failures.append(
                    "the managed alarms reported the states their "
                    "declared bindings select — the doctored leg "
                    "expected them never to report"
                )
            else:
                failures.append(
                    "the managed alarms never reported the states "
                    "their declared bindings select — "
                    + json.dumps(
                        {
                            f"p{100 + index}-{kind}": {
                                flag: bool_at(
                                    owner,
                                    points[f"{kind}_{index}_{flag}"],
                                )
                                for flag in (
                                    "shelved",
                                    "suppressed",
                                    "out_of_service",
                                )
                            }
                            for index in (1, 2)
                            for kind in ("fault", "thermal", "moisture")
                        },
                        sort_keys=True,
                    )
                )
            raise Abort
        evidence["managed_at"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "managed",
                "tick": evidence["managed_at"],
                "held_fault": managed_want(held, "fault", owner),
            }
        )

        # Phase 5 — the sibling's service: the next demand stages only
        # the sibling while the held pump's command stays released
        # through the whole cycle.
        def served(snapshot):
            demand = int_at(snapshot, points["demand_in"])
            if not isinstance(demand, int) or demand < 1:
                return None
            staged = int_at(snapshot, points["staged"])
            if (
                int_at(snapshot, points["duty"]) != sibling
                or staged != min(demand, 1)
                or bool_at(snapshot, points[f"cmd_{sibling}"]) is not True
                or bool_at(snapshot, points[f"cmd_{held}"]) is not False
                or bool_at(snapshot, points[f"avail_{held}"]) is not False
            ):
                return None
            return snapshot

        owner = drive_until(served, bound=CYCLE_BOUND, watch=watch)
        if owner is None:
            owner = tick()
            failures.append(
                "the sibling never served the demand with the held "
                "pump excluded — demand reads "
                f"{value(owner, points['demand_in'])}, duty "
                f"{value(owner, points['duty'])}, staged "
                f"{value(owner, points['staged'])}, the sibling's "
                f"cmd {value(owner, points[f'cmd_{sibling}'])}"
            )
            raise Abort
        evidence["served_at"] = owner["tick"]

        def completed(snapshot):
            return (
                int_at(snapshot, points["demand_in"]) == 0
                and int_at(snapshot, points["staged"]) == 0
                and int_at(snapshot, points["duty"]) == sibling
                and bool_at(snapshot, points[f"cmd_{held}"]) is False
            ) or None

        owner = drive_until(completed, bound=CYCLE_BOUND, watch=watch)
        if owner is None:
            owner = tick()
            failures.append(
                "the sibling's demand cycle never completed with the "
                "held pump still excluded — demand reads "
                f"{value(owner, points['demand_in'])}, staged "
                f"{value(owner, points['staged'])}, duty "
                f"{value(owner, points['duty'])}"
            )
            raise Abort
        evidence["completed_at"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "served",
                "served_at": evidence["served_at"],
                "completed_at": evidence["completed_at"],
            }
        )

        # Phase 6 — mid-OOS process truth: the injected non-Good on
        # the run contact defeats the feedback's proof; the managed
        # fault alarm's `alarm` reports the standing truth while
        # suppression withholds the `unacknowledged` latch — the named
        # without the annunciating.
        verdict = plant_io.request(
            {
                "op": "inject_fault",
                "point": points[f"run_{held}"],
                "fault": {"quality": BAD_QUALITY},
            }
        )
        if verdict.get("result") != "done":
            failures.append(
                f"inject_fault on the held pump's run contact "
                f"answered {verdict}"
            )
            raise Abort

        def truth(snapshot):
            if bool_at(snapshot, points[f"fault_{held}"]) is not True:
                return None
            for flag, want in (
                ("alarm", True),
                ("unacknowledged", False),
                ("suppressed", True),
                ("out_of_service", True),
            ):
                if (
                    bool_at(snapshot, points[f"fault_{held}_{flag}"])
                    is not want
                ):
                    return None
            if (
                bool_at(snapshot, points[f"cmd_{held}"]) is not False
                or int_at(snapshot, points["duty"]) != sibling
            ):
                return None
            return snapshot

        owner = drive_until(truth, bound=SETTLE_BOUND, watch=watch)
        if owner is None:
            owner = tick()
            failures.append(
                "the mid-OOS fault never landed the "
                "named-without-annunciating contract — `alarm` must "
                "report the process truth while suppression withholds "
                f"the latch: fault reads "
                f"{value(owner, points[f'fault_{held}'])}, alarm "
                f"{value(owner, points[f'fault_{held}_alarm'])}, "
                f"unacknowledged "
                f"{value(owner, points[f'fault_{held}_unacknowledged'])}"
            )
            raise Abort
        evidence["tripped_at"] = owner["tick"]
        digest_entries.append(
            {"phase": "truth", "tick": evidence["tripped_at"]}
        )

        # Phase 7 — the manual return under the standing fault: the
        # false write reopens the in-service leg, `avail` rejoins, and
        # suppression's release re-annunciates the outlasted trip —
        # the fresh `unacknowledged` the declared contract owes.
        release_write = write_value(points[f"oos_{held}"], False)
        submit(duty_url, release_write, failures)
        oos_window[0] = False

        def returned(snapshot):
            if bool_at(snapshot, points[f"oos_{held}"]) is not False:
                return None
            for key in (
                f"oos_ok_{held}",
                f"oos_ok_avail_{held}",
                f"oos_ok_guard_{held}",
                f"avail_{held}",
                f"avail_in_{held}",
            ):
                if bool_at(snapshot, points[key]) is not True:
                    return None
            for flag, want in (
                ("alarm", True),
                ("unacknowledged", True),
                ("suppressed", False),
                ("out_of_service", False),
            ):
                if (
                    bool_at(snapshot, points[f"fault_{held}_{flag}"])
                    is not want
                ):
                    return None
            return snapshot

        owner = drive_until(returned, bound=SETTLE_BOUND)
        if owner is None:
            owner = tick()
            failures.append(
                "the manual return never landed — the in-service "
                "leg, the avail rejoin, or the suppression-release "
                "re-annunciation missing: oos reads "
                f"{value(owner, points[f'oos_{held}'])}, avail "
                f"{value(owner, points[f'avail_{held}'])}, "
                f"unacknowledged "
                f"{value(owner, points[f'fault_{held}_unacknowledged'])}"
            )
            raise Abort
        evidence["returned_at"] = owner["tick"]
        release_receipt = settled_receipt(
            pair.get(f"{duty_url}/receipts", "GET /receipts", failures),
            release_write,
        )
        if release_receipt is None:
            failures.append(
                "the out-of-service release never settled applied "
                "into the adopted receipt log"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "returned",
                "tick": evidence["returned_at"],
                "applied": release_receipt["outcome"]["applied"]["tick"],
            }
        )

        # Phase 8 — the cleared contact and the receipted ack: the
        # proven fault clears once command and feedback agree, the
        # alarm returns, and the unacknowledged latch stands for the
        # receipted `ack` — then the ack input releases.
        verdict = plant_io.request(
            {"op": "clear_fault", "point": points[f"run_{held}"]}
        )
        if verdict.get("result") != "done":
            failures.append(
                f"clear_fault on the held pump's run contact answered "
                f"{verdict}"
            )
            raise Abort

        def cleared(snapshot):
            return (
                bool_at(snapshot, points[f"fault_{held}"]) is False
                and bool_at(snapshot, points[f"fault_{held}_alarm"])
                is False
                and bool_at(
                    snapshot, points[f"fault_{held}_unacknowledged"]
                )
                is True
            ) or None

        owner = drive_until(cleared, bound=SETTLE_BOUND)
        if owner is None:
            owner = tick()
            failures.append(
                "the cleared contact never landed — the latch must "
                f"hold unacknowledged until the receipted ack: fault "
                f"reads {value(owner, points[f'fault_{held}'])}, "
                f"alarm {value(owner, points[f'fault_{held}_alarm'])}, "
                f"unacknowledged "
                f"{value(owner, points[f'fault_{held}_unacknowledged'])}"
            )
            raise Abort

        ack_write = write_value(points[f"fault_{held}_ack"], True)
        submit(duty_url, ack_write, failures)
        owner = drive_until(
            lambda snapshot: bool_at(
                snapshot, points[f"fault_{held}_unacknowledged"]
            )
            is False
            or None
        )
        if owner is None:
            owner = tick()
            failures.append(
                "the receipted ack never cleared the unacknowledged "
                f"latch — it reads "
                f"{value(owner, points[f'fault_{held}_unacknowledged'])}"
            )
            raise Abort
        evidence["acknowledged_at"] = owner["tick"]
        ack_release = write_value(points[f"fault_{held}_ack"], False)
        submit(duty_url, ack_release, failures)
        owner = tick()
        digest_entries.append(
            {
                "phase": "cleared",
                "acknowledged_at": evidence["acknowledged_at"],
            }
        )

        # Phase 9 — rotation eligibility: the returned pump takes
        # `duty` at the next completed cycle under the declared
        # alternate-each-cycle policy and stages the demand — the
        # manual return proven as a return to service, not a quirk —
        # then the leg's whole window runs out with every driven input
        # standing restored.
        def rejoined(snapshot):
            staged = int_at(snapshot, points["staged"])
            return (
                int_at(snapshot, points["duty"]) == held
                and isinstance(staged, int)
                and staged >= 1
                and bool_at(snapshot, points[f"cmd_{held}"]) is True
            ) or None

        owner = drive_until(rejoined, bound=REJOIN_BOUND)
        if owner is None:
            owner = tick()
            failures.append(
                "the returned pump never rejoined the duty rotation — "
                "duty never named it again under the declared "
                f"alternate policy: duty reads "
                f"{value(owner, points['duty'])}, staged "
                f"{value(owner, points['staged'])}"
            )
            raise Abort
        evidence["rejoined_at"] = owner["tick"]
        owner = drive_until(
            lambda snapshot: int_at(snapshot, points["demand_in"]) == 0
            and int_at(snapshot, points["staged"]) == 0
            or None,
            bound=CYCLE_BOUND,
        )
        if owner is None:
            failures.append(
                "the rejoined pump's demand cycle never completed"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "rejoined",
                "rejoined_at": evidence["rejoined_at"],
                "completed_at": owner["tick"],
            }
        )
        evidence["final_tick"] = owner["tick"]

        # Phase 10 — the held-window invariants and the pair's roles:
        # the collected breaches fail the run, and the manifest's
        # declared arrangement must stand unmoved throughout — a
        # maintenance hold is a plant event, not a controller
        # failover.
        failures.extend(breaches)
        standby_role = pair.get(
            f"{standby_url}/role", "GET /role", failures
        )
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        sync = standby_role.get("sync")
        if standby_role.get("role") != "standby" or not (
            isinstance(sync, dict) and "tracking" in sync
        ):
            failures.append(
                "the standby's role moved through the out-of-service "
                f"run — GET /role answers {standby_role}"
            )
        if duty_role.get("role") != "active":
            failures.append(
                "the field owner's role moved through the "
                f"out-of-service run — GET /role answers {duty_role}"
            )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "roles",
                "duty_role": duty_role,
                "standby_role": standby_role,
            }
        )

        # Phase 11 — the receipted path: every submitted write settled
        # `applied` under the leg's actor into one adopted log both
        # peers serve identically.
        receipts_duty = pair.get(
            f"{duty_url}/receipts", "GET /receipts", failures
        )
        receipts_standby = pair.get(
            f"{standby_url}/receipts", "GET /receipts", failures
        )
        if receipts_duty != receipts_standby:
            failures.append(
                "the peers' receipt logs diverged — the adopted audit "
                "is not one log"
            )
            raise Abort
        for command in (oos_write, release_write, ack_write, ack_release):
            receipt = settled_receipt(receipts_duty, command)
            if receipt is None:
                failures.append(
                    f"the write {command} never settled applied into "
                    "the adopted receipt log"
                )
            elif receipt.get("actor") != ACTOR:
                failures.append(
                    f"the write {command} settled without the leg's "
                    f"actor — the receipt reads {receipt}"
                )
        if failures:
            raise Abort

        # Phase 12 — the durable record: the field owner's declared
        # journal file must carry the leg's transitions in `seq`
        # order beside the attributed receipts — the served journal
        # answering the same record, the journaled edges measuring the
        # declared wiring bound, and no flag the leg never drove ever
        # asserting.
        journal_path = rig.duty_files.get("journal_file")
        if journal_path is None or not os.path.exists(journal_path):
            failures.append(
                "the field owner's declared journal file does not "
                "exist — the --journal-file flag was not honored"
            )
            raise Abort
        records = pair.journal_records(journal_path)
        boundaries = [r for kind, r in records if kind == "boundary"]
        if boundaries != [{"run": 1, "tick": 0}]:
            failures.append(
                f"the field owner's journal boundaries are "
                f"{boundaries} — expected the single cold-start "
                "marker"
            )
            raise Abort
        entries = [r for kind, r in records if kind == "entry"]
        seqs = [entry["seq"] for entry in entries]
        if seqs != list(range(1, len(seqs) + 1)):
            failures.append(
                f"the field owner's journal seqs are not 1..n in "
                f"order: {seqs}"
            )
            raise Abort
        served = pair.get(f"{duty_url}/journal", "GET /journal", failures)
        events = journal_events(entries)
        if journal_events(served) != events:
            failures.append(
                "the served journal's transition stream diverges from "
                "the durable file's — the monitor does not answer the "
                "record it persists"
            )
            raise Abort
        settled_events = [
            event for event in events if event[0] == "settled"
        ]
        for command in (oos_write, release_write, ack_write, ack_release):
            point = command["write_value"]["point"]
            val = command["write_value"]["value"]
            want = ("settled", point, val, "applied", ACTOR)
            if want not in settled_events:
                failures.append(
                    f"the durable journal carries no attributed "
                    f"applied settlement for {want[1]} → {val}"
                )

        # The journaled edges measure the declared wiring bound — the
        # `oos` transition's tick to the aggregate's drop, the managed
        # flags' assertions, the release's rejoin — and the withheld
        # annunciation lands only on suppression's release under the
        # standing fault.
        edge_keys = {
            f"oos_{held}",
            f"avail_{held}",
            f"fault_{held}",
            "none_available",
        } | {
            f"fault_{held}_{flag}"
            for flag in (
                "alarm",
                "unacknowledged",
                "suppressed",
                "out_of_service",
            )
        }
        edges = {
            key: transitions(
                entries, points[key], anchor=evidence["baseline_at"]
            )
            for key in edge_keys
        }
        marks = {
            "oos_true": nth(edges[f"oos_{held}"], true),
            "oos_false": nth(edges[f"oos_{held}"], false),
            "avail_false": nth(edges[f"avail_{held}"], false),
            "avail_true": nth(edges[f"avail_{held}"], true),
            "oos_flag_on": nth(edges[f"fault_{held}_out_of_service"], true),
            "oos_flag_off": nth(edges[f"fault_{held}_out_of_service"], false),
            "supp_on": nth(edges[f"fault_{held}_suppressed"], true),
            "supp_off": nth(edges[f"fault_{held}_suppressed"], false),
            "fault_on": nth(edges[f"fault_{held}"], true),
            "fault_off": nth(edges[f"fault_{held}"], false),
            "alarm_on": nth(edges[f"fault_{held}_alarm"], true),
            "alarm_off": nth(edges[f"fault_{held}_alarm"], false),
            "unack_on": nth(edges[f"fault_{held}_unacknowledged"], true),
            "unack_off": nth(edges[f"fault_{held}_unacknowledged"], false),
        }
        missing = sorted(key for key, at in marks.items() if at is None)
        if missing:
            failures.append(
                "the durable journal never recorded "
                + ", ".join(missing)
                + f" — the leg's edges read {json.dumps(edges, sort_keys=True)[:400]}"
            )
        else:
            if not marks["oos_true"] <= marks["avail_false"] <= (
                marks["oos_true"] + OOS_BOUND_TICKS
            ):
                failures.append(
                    f"the avail drop journaled at tick "
                    f"{marks['avail_false']} — "
                    f"{marks['avail_false'] - marks['oos_true']} ticks "
                    f"from the oos write at {marks['oos_true']}, "
                    f"outside the declared wiring bound "
                    f"{OOS_BOUND_TICKS}"
                )
            if marks["oos_flag_on"] - marks["oos_true"] > OOS_BOUND_TICKS or (
                marks["supp_on"] - marks["oos_true"] > OOS_BOUND_TICKS
            ):
                failures.append(
                    "the managed flags journaled their assertions "
                    "beyond the declared wiring bound — "
                    + json.dumps(marks, sort_keys=True)
                )
            if not marks["oos_false"] <= marks["avail_true"] <= (
                marks["oos_false"] + OOS_BOUND_TICKS
            ):
                failures.append(
                    f"the avail rejoin journaled at tick "
                    f"{marks['avail_true']} — "
                    f"{marks['avail_true'] - marks['oos_false']} ticks "
                    f"from the release at {marks['oos_false']}, "
                    f"outside the declared wiring bound "
                    f"{OOS_BOUND_TICKS}"
                )
            if not marks["fault_on"] <= marks["alarm_on"] <= (
                marks["fault_on"] + OOS_BOUND_TICKS
            ):
                failures.append(
                    f"the fault alarm did not follow the proven "
                    f"fault inside the carrier hop — fault at tick "
                    f"{marks['fault_on']}, alarm at "
                    f"{marks['alarm_on']}"
                )
            if marks["unack_on"] <= marks["fault_on"]:
                failures.append(
                    "the mid-OOS fault annunciated before suppression "
                    "released — the named-without-annunciating "
                    "contract broken (fault at "
                    f"{marks['fault_on']}, unacknowledged at "
                    f"{marks['unack_on']})"
                )
            if not (
                marks["supp_off"]
                <= marks["unack_on"]
                <= marks["unack_off"]
            ):
                failures.append(
                    "the withheld annunciation did not land on "
                    "suppression's release (supp_off "
                    f"{marks['supp_off']}, unacknowledged "
                    f"{marks['unack_on']} → {marks['unack_off']})"
                )

        # The ordered record the leg drove, per journaled point: the
        # hold and release, the availability drop and rejoin, the
        # fault prove and clear, and each managed flag whose declared
        # binding the leg's writes drive asserting and releasing in
        # order — while an unbound lifecycle input, a sibling-bound
        # input, or `none_available` can never assert.
        ordered = {
            f"oos_{held}": [true, false],
            f"avail_{held}": [false, true],
            f"fault_{held}": [true, false],
            f"fault_{held}_alarm": [true, false],
            f"fault_{held}_unacknowledged": [true, false],
        }
        never_true = ["none_available"]
        for index in (1, 2):
            for kind in ("fault", "thermal", "moisture"):
                ports = managed[(index, kind)]
                for flag, port in (
                    ("shelved", "shelve"),
                    ("suppressed", "suppress"),
                    ("out_of_service", "oos"),
                ):
                    key = f"{kind}_{index}_{flag}"
                    bound = ports.get(port)
                    driven = bound in (
                        points[f"oos_{index}"],
                        points[f"fault_sup_in_{index}"],
                    )
                    if bound is None or (driven and index != held):
                        never_true.append(key)
                    elif driven:
                        ordered[key] = [true, false]
                    # A lifecycle input bound to a driver outside the
                    # leg's model is left unchecked — the declared
                    # precedence, never an assumed one.
                if not (index == held and kind == "fault"):
                    never_true.extend(
                        [
                            f"{kind}_{index}_alarm",
                            f"{kind}_{index}_unacknowledged",
                        ]
                    )
        changed = {}
        for kind, point, *rest in events:
            if kind in ("changed", "quality"):
                changed.setdefault(point, []).append(rest[0])
        for key, wanted in ordered.items():
            if not subsequence(wanted, changed.get(points[key], [])):
                failures.append(
                    f"point {points[key]} never journaled the ordered "
                    f"{key} transitions: "
                    f"{json.dumps(changed.get(points[key], []))[:200]}"
                )
        for key in never_true:
            if true in changed.get(points[key], []):
                failures.append(
                    f"point {points[key]} ({key}) journaled a true "
                    "transition the leg never drove"
                )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "audit",
                "duty_role": duty_role,
                "standby_role": standby_role,
                "events": events,
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
        choices=["keeps-duty", "managed-silent"],
        help="doctor the leg's own expectation — the pass must fail "
        "naming the evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = oos_pass(args, args.tamper)
    except Abort as abort:
        for line in abort.args:
            eprint(f"oos: {line}")
        return 1
    for failure in failures:
        eprint(f"oos: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"oos: the {args.tamper} case passed silently — the "
                "leg never noticed the doctored expectation"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"oos-digest {digest} — duty handed to the sibling at tick "
        f"{evidence['excluded_at']}, managed states at tick "
        f"{evidence['managed_at']}, served at tick "
        f"{evidence['served_at']}, returned at tick "
        f"{evidence['returned_at']}, acknowledged at tick "
        f"{evidence['acknowledged_at']}, rejoined at tick "
        f"{evidence['rejoined_at']}, roles unmoved through tick "
        f"{evidence['final_tick']}, {evidence['entries']} journal "
        "entries"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
