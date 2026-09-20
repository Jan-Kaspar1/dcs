#!/usr/bin/env python3
"""The `dcs-alarm-report` leg for the reference plant — the
consumer-side proof that the released alarm flood and performance tool
computes the declared metric set (WW-ALM-004) over the deployed pair's
durable alarm record, from released artifacts alone (WW-ENG-003).

The burst leg proves the record's first-out ordering; this leg proves
the tooling-side computation the flood-and-performance decision
promises a customer: `dcs-alarm-report` — the `dcs-monitor` binary —
reads the served monitoring surface and the durable journal file and
prints the `AlarmReport` metric set, adding no served endpoint. The
rig reads the standby wiring and persistence fields out of
`deploy/manifest.json` and spawns the released tooling exactly as the
pair legs do. The run:

- converges the declared standby to `tracking`, then drives one
  managed alarm through its lifecycle — a non-Good quality on
  `level-primary` annunciating the failover's managed alarm, a
  receipted `ack` write through the active's `POST /command` pairing
  the annunciation to its attributed acknowledgment, and the cleared
  instrument returning the alarm — so the durable record carries one
  measured activation/annunciation/ack/return episode;
- runs `dcs-alarm-report <addr>` against the field owner's monitor —
  the served-journal report — asserting the emitted model's whole
  alarm set computed per instance, the driven lifecycle's measured
  counts, the ack's response pair attributed to the leg's actor, and
  the report's cross-section consistency;
- runs `dcs-alarm-report <addr> --journal-file <path>` over the field
  owner's manifest-declared durable journal file — the
  restart-surviving dataset — asserting the file's report answers the
  served report's metric set identically, only its run-boundary
  accounting added;
- exercises the tool's refusal modes: an unreachable monitor and an
  unreadable journal file each exit nonzero naming the failure —
  never a silent pass.

Usage:

    report.py --alarm-report PATH --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `report-digest <sha256>` line prints — the check runs
two passes and compares them (`report-nondeterministic`). A contract
violation reports `report: …` lines on stderr and exits 1 — the
check's `report-failed`. The tampers doctor the leg's inputs — each
must fail the pass naming its evidence rather than pass silently:
`expect-quiet` asserts the driven alarm left no activation, so the
honest report's measured count must fail the leg naming the actual
count; `unreachable-monitor` points the report at a dead address, so
the invocation must fail naming the refused fetch; and
`unreadable-journal` points `--journal-file` at a path that does not
exist, so the invocation must fail naming the unreadable file.
"""

import argparse
import hashlib
import json
import subprocess
import sys

import pair
import simulate


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The bound each driven phase gets to land its declared effect across
# the wiring's one-scan carrier crossings. The actor the leg's
# receipted submissions declare — the attribution the report's
# response pair carries.
SETTLE_BOUND = 16
ACTOR = "ci-report"

# The injected non-Good the fault surface carries — the same quality
# the burst leg's cascade drives.
BAD_QUALITY = {"bad": "device_fault"}

# The signal names resolving the leg's driven points out of the
# emitted model — the same names the signal index serves, so the leg
# exercises the declared seam, never a hard-coded id.
SIGNALS = {
    "level_primary": "level-primary",
    "backup_active": "backup-active",
    "backup_alarm": "backup-active-alarm",
    "backup_unack": "backup-active-unacknowledged",
}


def signal_points(model):
    """The `{key: point id}` map the emitted model's signal index
    declares for the leg's driven alarm — None when the model declares
    no such alarm. A signal's `source` is the point it names; the
    lowest-signal-id-wins rule the served index applies."""
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
    return points


def declared_alarms(model):
    """The alarm instances the emitted model declares — every
    component carrying a status `alarm` `Out` port bound to a point —
    as `{<kind>:<id>: {"alarm", "signal", "ack", "priority",
    "response_ticks"}}`. The same descriptor-to-point join the tool's
    `alarm_instances` performs, resolved here from the document so the
    leg proves the report against the artifact. `signal` is the
    `alarm` point's signal name under the index's lowest-id-wins rule;
    `ack` is the `In` port's bound point, `None` when the kind
    declares none; `priority`/`response_ticks` are the declared
    rationalization parameters the report reads."""
    bound = simulate.bound_points(model)
    by_point = {}
    for signal in model.get("signals", []):
        current = by_point.get(signal["source"])
        if current is None or signal["id"] < current["id"]:
            by_point[signal["source"]] = signal
    alarms = {}
    for component in model.get("components", []):
        ports = component.get("ports", {})
        alarm = ports.get("alarm")
        if not isinstance(alarm, dict) or alarm.get("direction") != "out":
            continue
        alarm_point = bound.get((component["id"], "alarm"))
        if alarm_point is None:
            continue
        signal = by_point.get(alarm_point)
        declared_int = lambda name: component.get("parameters", {}).get(
            name, {}
        ).get("int")
        alarms[f"{component['kind']}:{component['id']}"] = {
            "alarm": alarm_point,
            "signal": signal["name"] if signal else f"point-{alarm_point}",
            "ack": bound.get((component["id"], "ack")),
            "priority": declared_int("priority"),
            "response_ticks": declared_int("response_ticks"),
        }
    return alarms


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


def tick(rig, failures):
    """One driven pair tick — the tracking peer scanned first so its
    pull applies the owner's latest checkpoint, then the field owner —
    asserting the peers' images stay identical. Returns the owner's
    served snapshot."""
    return rig.tick(rig.standby_url, rig.duty_url, failures)[1]


def drive_until(rig, failures, condition):
    """Driven pair ticks until `condition(owner_snapshot)` holds —
    returns the satisfying snapshot, or None when SETTLE_BOUND scans
    pass without it landing."""
    for _ in range(SETTLE_BOUND):
        owner = tick(rig, failures)
        if condition(owner):
            return owner
    return None


def run_report(binary, arguments):
    """One `dcs-alarm-report` invocation; returns the
    CompletedProcess."""
    return subprocess.run(
        [binary, *arguments],
        capture_output=True,
        text=True,
        timeout=30,
    )


def report_mismatches(report, declared, driven, tamper, failures):
    """The served-journal report's metric assertions — the emitted
    model's whole alarm set computed per instance, the driven
    lifecycle's measured counts, and the cross-section consistency the
    declared metric set owes. `driven` names the lifecycle instance
    `(component, signal)`; `tamper` doctors the activation
    expectation."""
    alarms = {
        entry.get("component"): entry for entry in report.get("alarms", [])
    }
    for name, want in declared.items():
        metrics = alarms.get(name)
        if metrics is None:
            failures.append(
                f"the report computes no metrics for the declared "
                f"alarm {name} — the emitted model's alarm set is "
                "not covered"
            )
            continue
        if metrics.get("signal") != want["signal"]:
            failures.append(
                f"{name}: the report's alarm signal reads "
                f"{metrics.get('signal')!r}, declared "
                f"{want['signal']!r}"
            )
        if metrics.get("alarm_point") != want["alarm"]:
            failures.append(
                f"{name}: the report watches point "
                f"{metrics.get('alarm_point')}, declared "
                f"{want['alarm']}"
            )
        if metrics.get("priority") != want["priority"]:
            failures.append(
                f"{name}: the report's priority reads "
                f"{metrics.get('priority')!r}, declared "
                f"{want['priority']!r}"
            )
        if metrics.get("response_ticks") != want["response_ticks"]:
            failures.append(
                f"{name}: the report's response_ticks reads "
                f"{metrics.get('response_ticks')!r}, declared "
                f"{want['response_ticks']!r}"
            )
    component, signal = driven
    metrics = alarms.get(component)
    if metrics is None:
        failures.append(
            f"the report computes no metrics for the driven alarm "
            f"{component} — the measured lifecycle is absent"
        )
        return
    if metrics.get("signal") != signal:
        failures.append(
            f"{component}: the driven alarm's signal reads "
            f"{metrics.get('signal')!r}, declared {signal!r}"
        )
    activations = metrics.get("activations")
    if tamper == "expect-quiet":
        if activations != 0:
            failures.append(
                f"{component}: expected zero activations, the report "
                f"counts {activations}"
            )
    elif activations != 1:
        failures.append(
            f"{component}: the driven lifecycle counts "
            f"{activations} activations, expected the one the leg "
            "drove"
        )
    if metrics.get("annunciations") != 1:
        failures.append(
            f"{component}: the driven lifecycle counts "
            f"{metrics.get('annunciations')} annunciations, "
            "expected the one its latch raised"
        )
    if metrics.get("acknowledgments") != 1:
        failures.append(
            f"{component}: the report pairs "
            f"{metrics.get('acknowledgments')} acknowledgments, "
            "expected the receipted ack's one"
        )
    rates = report.get("rates", {})
    if rates.get("activations") != sum(
        entry.get("activations", 0) for entry in alarms.values()
    ):
        failures.append(
            f"the rates section counts {rates.get('activations')} "
            "activations against the per-instance detail's "
            f"{sum(entry.get('activations', 0) for entry in alarms.values())}"
        )
    if rates.get("annunciations") != sum(
        entry.get("annunciations", 0) for entry in alarms.values()
    ):
        failures.append(
            f"the rates section counts {rates.get('annunciations')} "
            "annunciations against the per-instance detail's "
            f"{sum(entry.get('annunciations', 0) for entry in alarms.values())}"
        )
    distribution = report.get("priority_distribution", [])
    if sum(entry.get("annunciations", 0) for entry in distribution) != rates.get(
        "annunciations"
    ):
        failures.append(
            "the priority distribution's buckets do not cover the "
            "record's annunciations"
        )
    responses = report.get("responses", {})
    pair_ = next(
        (
            entry
            for entry in responses.get("pairs", [])
            if entry.get("component") == component
        ),
        None,
    )
    if pair_ is None:
        failures.append(
            f"{component}: the receipted ack formed no response pair "
            "— the annunciation-to-acknowledgment measure is absent"
        )
    else:
        if pair_.get("ticks") != pair_.get("applied_tick", 0) - pair_.get(
            "annunciation_tick", 0
        ):
            failures.append(
                f"{component}: the response pair's ticks "
                f"{pair_.get('ticks')} disagree with its applied and "
                "annunciation ticks"
            )
        if pair_.get("actor") != ACTOR:
            failures.append(
                f"{component}: the response pair carries actor "
                f"{pair_.get('actor')!r}, expected {ACTOR!r} — the "
                "receipted path's attribution"
            )
        if pair_.get("within_declared") is not True:
            failures.append(
                f"{component}: the response pair answers "
                f"within_declared={pair_.get('within_declared')!r} "
                "against the instance's declared response_ticks"
            )
    for entry in report.get("standing", []):
        if entry.get("component") not in declared:
            failures.append(
                f"a standing alarm names {entry.get('component')!r}, "
                "which the emitted model declares no alarm for"
            )
        if entry.get("component") == component:
            failures.append(
                f"{component} still stands after the cleared "
                "instrument — the returned alarm lingers"
            )


def report_pass(args, tamper):
    """The report run: converge, drive one alarm lifecycle, compute
    the served and durable-file reports, exercise the refusal modes.
    Returns `(digest_entries, evidence, failures)`."""
    declared_pair = pair.manifest_pair(args.manifest)
    if declared_pair is None:
        raise Abort(
            "the manifest declares no standby pair — the report leg "
            "has nothing to exercise"
        )
    _manifest, duty_decl, _standby_decl = declared_pair
    with open(args.model) as handle:
        model = json.load(handle)
    points = signal_points(model)
    if points is None:
        raise Abort(
            "the emitted model declares no backup-source alarm — the "
            "report leg has nothing to drive"
        )
    declared = declared_alarms(model)
    if not declared:
        raise Abort(
            "the emitted model declares no alarm set — the report leg "
            "has nothing to measure"
        )
    component = next(
        (
            name
            for name, entry in declared.items()
            if entry["alarm"] == points["backup_alarm"]
        ),
        None,
    )
    if component is None:
        raise Abort(
            "no declared alarm watches the backup-active point — the "
            "report leg has nothing to drive"
        )
    ack_point = declared[component]["ack"]
    writable = {
        point["id"] for point in model.get("io_points", []) if point.get("writable")
    }
    if ack_point is None or ack_point not in writable:
        raise Abort(
            f"{component}'s ack input binds no model-declared writable "
            "point — the leg's acknowledgment has no receipted seam"
        )
    driven = (component, declared[component]["signal"])
    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared_pair)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        plant_io = rig.plant_io

        # Phase 1 — convergence: the pair leg's driven-tick loop, the
        # tracking peer scanned first so each pull applies the owner's
        # latest checkpoint and the peers rest identical.
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

        # Phase 2 — the driven lifecycle: a non-Good quality on
        # `level-primary` fails the measurement over so the managed
        # alarm annunciates; the receipted `ack` write pairs the
        # annunciation to its attributed acknowledgment; the cleared
        # instrument returns the alarm.
        verdict = plant_io.request(
            {
                "op": "inject_fault",
                "point": points["level_primary"],
                "fault": {"quality": BAD_QUALITY},
            }
        )
        if verdict.get("result") != "done":
            failures.append(
                f"inject_fault on level-primary answered {verdict}"
            )
            raise Abort
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: value(snapshot, points["backup_active"])
            == {"bool": True}
            and value(snapshot, points["backup_alarm"]) == {"bool": True}
            and value(snapshot, points["backup_unack"]) == {"bool": True},
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                "the level-primary fault never annunciated — "
                f"backup-active reads "
                f"{value(owner, points['backup_active'])}, alarm "
                f"{value(owner, points['backup_alarm'])}, "
                f"unacknowledged "
                f"{value(owner, points['backup_unack'])}"
            )
            raise Abort
        evidence["annunciated_at"] = owner["tick"]
        submit(duty_url, write_value(ack_point, True), failures)
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: value(snapshot, points["backup_unack"])
            == {"bool": False},
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                "the receipted ack never cleared the latch — "
                f"unacknowledged reads "
                f"{value(owner, points['backup_unack'])}"
            )
            raise Abort
        evidence["acknowledged_at"] = owner["tick"]
        submit(duty_url, write_value(ack_point, False), failures)
        owner = tick(rig, failures)
        verdict = plant_io.request(
            {"op": "clear_fault", "point": points["level_primary"]}
        )
        if verdict.get("result") != "done":
            failures.append(f"clear_fault on level-primary answered {verdict}")
            raise Abort
        owner = drive_until(
            rig,
            failures,
            lambda snapshot: value(snapshot, points["backup_active"])
            == {"bool": False}
            and value(snapshot, points["backup_alarm"]) == {"bool": False},
        )
        if owner is None:
            owner = tick(rig, failures)
            failures.append(
                "the cleared instrument left the alarm standing — "
                f"backup-active reads "
                f"{value(owner, points['backup_active'])}, alarm "
                f"{value(owner, points['backup_alarm'])}"
            )
            raise Abort
        evidence["returned_at"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "lifecycle",
                "component": component,
                "annunciated_at": evidence["annunciated_at"],
                "acknowledged_at": evidence["acknowledged_at"],
                "returned_at": evidence["returned_at"],
            }
        )

        # Phase 3 — the served-journal report: `dcs-alarm-report
        # <addr>` computes the declared metric set over the field
        # owner's `GET /journal` — the live, bounded view. The
        # unreachable-monitor tamper doctors the leg's report target —
        # the invocation must fail naming the refused fetch.
        addr = duty_url.removeprefix("http://")
        report_addr = (
            pair.closed_port() if tamper == "unreachable-monitor" else addr
        )
        result = run_report(args.alarm_report, [report_addr])
        if result.returncode != 0:
            label = (
                "the unreachable monitor's report"
                if tamper == "unreachable-monitor"
                else "the served-journal report"
            )
            failures.append(
                f"{label}: dcs-alarm-report {report_addr} exited "
                f"{result.returncode}: {result.stderr.strip()}"
            )
            raise Abort
        try:
            served = json.loads(result.stdout)
        except json.JSONDecodeError:
            failures.append(
                "dcs-alarm-report printed no AlarmReport document"
            )
            raise Abort
        report_mismatches(served, declared, driven, tamper, failures)
        source = served.get("source", {})
        if source.get("journal_runs") is not None:
            failures.append(
                "the served journal's report carries run-boundary "
                "accounting — the bounded view declares no lifetimes"
            )
        journal = pair.get(f"{duty_url}/journal", "GET /journal", failures)
        if source.get("journal_entries") != len(journal):
            failures.append(
                f"the report consumed {source.get('journal_entries')} "
                f"journal entries where the monitor serves {len(journal)}"
            )
        elif journal:
            if source.get("first_seq") != journal[0].get("seq") or source.get(
                "last_seq"
            ) != journal[-1].get("seq"):
                failures.append(
                    "the report's journal stretch disagrees with the "
                    "served record's first and last seqs"
                )
        if failures:
            raise Abort
        digest_entries.append({"phase": "served", "report": served})

        # Phase 4 — the durable-file report: the same computation over
        # the field owner's manifest-declared `--journal-file` — the
        # restart-surviving dataset. The file and the bounded view are
        # one record for this run, so the reports answer the identical
        # metric set — only the file's run-boundary accounting added.
        # The unreadable-journal tamper doctors the declared path —
        # the invocation must fail naming the unreadable file.
        journal_path = rig.duty_files.get("journal_file")
        if journal_path is None:
            failures.append(
                "the manifest declares no journal file for the field "
                "owner — the durable-file report has nothing to read"
            )
            raise Abort
        read_path = (
            f"{rig.scratch}/no-such-journal.jsonl"
            if tamper == "unreadable-journal"
            else journal_path
        )
        result = run_report(
            args.alarm_report,
            [addr, "--journal-file", read_path],
        )
        if result.returncode != 0:
            label = (
                "the unreadable journal file's report"
                if tamper == "unreadable-journal"
                else "the durable-file report"
            )
            failures.append(
                f"{label}: dcs-alarm-report {addr} --journal-file "
                f"{read_path} exited {result.returncode}: "
                f"{result.stderr.strip()}"
            )
            raise Abort
        try:
            filed = json.loads(result.stdout)
        except json.JSONDecodeError:
            failures.append(
                "the journal-file run printed no AlarmReport document"
            )
            raise Abort
        file_source = filed.get("source", {})
        if file_source.get("journal_runs") != 1:
            failures.append(
                f"the durable file's report counts "
                f"{file_source.get('journal_runs')} journal runs, "
                "expected the run's single lifetime"
            )
        stripped = {
            **filed,
            "source": {**file_source, "journal_runs": None},
        }
        if stripped != served:
            failures.append(
                "the durable file's report diverges from the served "
                "journal's — the same record answered different "
                "metrics"
            )
        if failures:
            raise Abort
        digest_entries.append({"phase": "file", "report": filed})

        # Phase 5 — the refusal modes: each must exit nonzero naming
        # the failure — never a silent pass. The record carries the
        # named observation, kept free of the per-run addresses and
        # scratch paths the stderr strings name.
        refusals = []
        result = run_report(args.alarm_report, [pair.closed_port()])
        if result.returncode == 0:
            failures.append(
                "dcs-alarm-report against an unreachable monitor "
                "exited zero — the refusal mode passed silently"
            )
        elif "snapshot" not in result.stderr:
            failures.append(
                f"the unreachable monitor's failure names "
                f"{result.stderr.strip()!r}, not the refused fetch"
            )
        else:
            refusals.append(
                {
                    "mode": "unreachable-monitor",
                    "exit": result.returncode,
                    "names": "snapshot",
                }
            )
        missing = f"{rig.scratch}/no-such-journal.jsonl"
        result = run_report(
            args.alarm_report, [addr, "--journal-file", missing]
        )
        if result.returncode == 0:
            failures.append(
                "dcs-alarm-report against an unreadable journal file "
                "exited zero — the refusal mode passed silently"
            )
        elif "cannot read journal file" not in result.stderr:
            failures.append(
                f"the unreadable journal file's failure names "
                f"{result.stderr.strip()!r}, not the file's refusal"
            )
        else:
            refusals.append(
                {
                    "mode": "unreadable-journal-file",
                    "exit": result.returncode,
                    "names": "cannot read journal file",
                }
            )
        if failures:
            raise Abort
        digest_entries.append({"phase": "refusals", "observed": refusals})
        evidence["instances"] = len(declared)
        evidence["activations"] = served["rates"]["activations"]
        evidence["pairs"] = len(served["responses"]["pairs"])
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
    parser.add_argument("--alarm-report", required=True)
    parser.add_argument("--plant-server", required=True)
    parser.add_argument("--controller", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--dynamics", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument(
        "--tamper",
        choices=["expect-quiet", "unreachable-monitor", "unreadable-journal"],
        help="doctor the leg's inputs — the pass must fail naming the "
        "evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = report_pass(args, args.tamper)
    except Abort as abort:
        for line in abort.args:
            eprint(f"report: {line}")
        return 1
    for failure in failures:
        eprint(f"report: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"report: the {args.tamper} case passed silently — the "
                "leg never noticed the doctored input"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"report-digest {digest} — tracking by tick "
        f"{evidence['converged']}, annunciated at tick "
        f"{evidence['annunciated_at']}, acknowledged at tick "
        f"{evidence['acknowledged_at']}, returned at tick "
        f"{evidence['returned_at']}, {evidence['instances']} alarm "
        f"instances, {evidence['activations']} activations, "
        f"{evidence['pairs']} response pairs"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
