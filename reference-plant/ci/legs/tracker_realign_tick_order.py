#!/usr/bin/env python3
"""The tracker-realign-tick-order leg for the reference plant — the
consumer-side proof that a tracking peer's durable journal stays
tick-ordered across a realign from a degraded window: within a run no
journaled entry stamps a tick below an earlier entry's (WW-ENG-003,
WW-LCM-001, WW-FND-004 — the attributed durable-record clause the
#830 fix established, mirrored at the customer boundary from the qa
rig's tracker-realign-tick-order leg).

The defect the contract closed had the peer's durable journal stamping
entries out of tick order after realigning from a held window: the
frozen source left the tracker's own run clock pacing ahead while its
degraded scans journaled transition marks at that held tick, and the
realigning apply's covering checkpoint carried a settled receipt whose
line-apply tick sat below the standing mark — the adopted
`command_settled` journaled at the carried tick and the journal's axis
rewound. The fix stamps every journaled entry at the run's own standing
tick — the axis never rewinds, the receipt's carried tick preserved
inside the record — and this leg exercises exactly that shape on the
manifest-declared pair:

- converges the declared pair to its launch roles — the field owner
  `active`, the standby `tracking` — through the pair rig's
  driven-tick loop, and gates the contract's surface: the standby's
  declared durable `--journal-file` must exist, parse, and carry
  `entry` records on an integer tick axis — a pinned release predating
  the attributed durable-record contract reports
  `realign-tick-order-digest inconclusive`, never a failure;
- submits one receipted `write_value` command on the field owner —
  the admission the covering checkpoint must carry to the realigning
  peer;
- freezes the field owner — `SIGSTOP` on the spawned process, the
  container-pause lever the command-abort-verdict leg drives — so
  the tracking peer's per-scan checkpoint pulls produce nothing and
  its sync verdict goes `degraded` while its own run clock, and the
  transitions it journals, run ahead of the frozen line. A plant-side
  quality fault on a declared-`journaled` input gives the held window
  its transition traffic — the marks the run's tick axis carries
  ahead of the line. The pair launches unarmed: the leg needs the
  standby held standby through the miss run, never the failover gate
  the armed-budget legs exercise;
- thaws the owner and drives the pair past the realign: the owner's
  first post-thaw scan applies the queued admission, the covering
  checkpoint carries the settled receipt, and the realigning apply
  adopts it — the journaled `command_settled` whose stamp is exactly
  the carried-tick surface the defect rewound;
- audits the standby's durable journal: within the run every entry's
  recorded tick must be at or after its predecessor's, the adopted
  settle journaled exactly once behind the window's marks, and the
  pair stands back on its launch roles — the field owner `active`,
  the standby `tracking`. The plant-side fault clears first so the
  recovery transition also lands inside the audited axis.

Usage:

    tracker_realign_tick_order.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `realign-tick-order-digest <sha256>` line prints — the
check runs two passes and compares them
(`tracker-realign-tick-order-nondeterministic`). A contract violation
reports `realign-tick-order: …` lines on stderr and exits 1 — the
check's `realign-tick-order-failed`. `--tamper regressed-tick` doctors
the adopted settle's durable stamp back to the carried line tick — the
defect's own rewind — so the leg proves its tick-order assertion fires
rather than passing an unexercised contract.
"""

import argparse
import json
import os
import signal
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import claim_reclaim
import pair
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored case.
# The doctored case: a leg asserting the journal ordered while an
# entry stamps a regressed tick — the adopted settle stamped at the
# carried line tick, the defect's own rewind — must surface the
# named diagnostic rather than passing an unexercised contract.
LEG = {
    "order": 600,
    "title": "the tracker-realign-tick-order leg",
    "passes": "realign-tick-order-leg",
    "failed": "realign-tick-order-failed",
    "tampers": [
        {
            "name": "regressed-tick",
            "passed": "a regressed-tick case passed the tracker-realign-tick-order leg",
            "missed": "the regressed-tick case did not report its named diagnostic",
            "evidence": ["tick axis regressed across the realign"],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The pinned release predates — or the harness admits no lever
    for — the contract the leg exercises: the durable journal's
    integer tick axis, the stop/pause lever the frozen-source window
    is staged through, the plant-side fault surface the window's
    transition traffic lands on, or the reconvergence the audit is
    staged across never arrived under the run's own staging. The run
    classifies inconclusive, never a product failure."""


# The driven-scan bounds each phase runs: the held window's fixed
# standby scans — each a produced-nothing pull under the frozen
# source, the run's clock pacing ahead of the line while the injected
# transition journals at the standing tick — the post-thaw
# reconvergence bound covering a pull still consuming the window's
# stale in-flight fetch, and the adopted settle's landing window. The
# actor the leg's receipted submission declares, and the injected
# non-Good the fault surface carries — the same quality the
# burst-order leg's protection drive uses.
WINDOW_SCANS = 6
REALIGN_SCANS = 8
ADOPT_SCANS = 4
RECOVERY_SCANS = 2
ACTOR = "ci-realign-tick-order"
BAD_QUALITY = {"bad": "device_fault"}


def writable_bool_point(model):
    """The lowest-id writable boolean `in` point the emitted model
    declares — the receipted `write_value` target the leg's admission
    carries. `requires_reason`-marked points are excluded: the leg
    submits reasonless."""
    points = [
        point["id"]
        for point in sorted(model["io_points"], key=lambda entry: entry["id"])
        if point.get("writable")
        and point["direction"] == "in"
        and point["value_type"] == "bool"
        and not point.get("requires_reason")
    ]
    return points[0] if points else None


def journaled_bool_point(model, exclude):
    """The lowest-id declared-`journaled` channel-bound boolean `in`
    point — the quality-fault target whose injected transitions the
    held window journals `quality_changed` on both peers; the plant
    driver's fault surface only reaches channel-bound points.
    `exclude` keeps the mark distinct from the command's target."""
    candidates = [
        point["id"]
        for point in sorted(model["io_points"], key=lambda entry: entry["id"])
        if point.get("journaled")
        and point["direction"] == "in"
        and point["value_type"] == "bool"
        and point.get("channel") is not None
        and point["id"] not in exclude
    ]
    return candidates[0] if candidates else None


def pause_peer(process):
    """The field owner's container pause — `SIGSTOP` on the spawned
    process, the `docker pause` reproduction's lever beside
    `pair.stop`'s container stop: the monitor's listener stays bound
    but unanswered, so the tracking peer's checkpoint fetches drop to
    produced-nothing misses while its own scan runs ahead. A lever
    that cannot land classifies inconclusive."""
    if not hasattr(signal, "SIGSTOP"):
        raise Inconclusive(
            "the consumer harness admits no stop/pause lever — no "
            "SIGSTOP"
        )
    try:
        process.send_signal(signal.SIGSTOP)
    except Exception as error:
        raise Inconclusive(
            f"the field-owner pause lever never landed: {error}"
        )


def resume_peer(process):
    """The pause's restore — `SIGCONT` so the frozen source serves
    again, the `docker unpause` the reproduction runs. Returns None
    on landing, the error string on refusal."""
    try:
        process.send_signal(signal.SIGCONT)
        return None
    except Exception as error:
        return str(error)


def try_role(url):
    """The served /role report, or None when the peer does not answer
    — a dropped observation, never an unwind."""
    try:
        return simulate.http(f"{url}/role")
    except Exception:
        return None


def role_row(url):
    """One normalized watch row off the tracking peer's served
    /role — the observation the held-window audit replays: the
    reported role and the sync verdict's kind."""
    report = try_role(url)
    if report is None:
        return None
    return {
        "role": report.get("role"),
        "sync": claim_reclaim.sync_state(report),
        "failover": report.get("failover"),
    }


def clean_error(error, rig):
    """An error string normalized for the digested output — the run's
    runner-owned scratch paths replaced so two passes on different
    scratch directories read identically."""
    return str(error).replace(rig.scratch, "<run>")[:300]


def run_entries(path):
    """The durable `entry` records the journal file carries in the
    current run — the records after the last run-boundary marker (the
    whole file when none has landed yet)."""
    entries = []
    for kind, record in pair.journal_records(path):
        if kind == "boundary":
            entries = []
        else:
            entries.append(record)
    return entries


def axis_audit(entries):
    """The run's tick-axis verdict over one durable entry list: the
    integer-tick records held against the running mark — the
    non-regressing append axis the #830 fix's standing stamp owes —
    each entry stamping a tick below the standing mark recorded a
    regression, each record carrying no integer tick a malformed
    count."""
    ticks = []
    malformed = 0
    for entry in entries:
        tick = entry.get("tick")
        if not isinstance(tick, int) or isinstance(tick, bool):
            malformed += 1
            continue
        ticks.append(entry)
    regressions = []
    mark = None
    for entry in ticks:
        if mark is not None and entry["tick"] < mark:
            regressions.append(
                {
                    "seq": entry.get("seq"),
                    "tick": entry["tick"],
                    "below": mark,
                }
            )
        else:
            mark = entry["tick"]
    return {
        "entries": len(ticks),
        "regressions": regressions,
        "malformed": malformed,
    }


def adopted_settles(entries, command):
    """The `command_settled` entries a journal tail carries for the
    leg's admission — the covering checkpoint's carried receipt the
    realign lands — matched on the (command, actor) pair."""
    found = []
    for entry in entries:
        settled = (entry.get("event") or {}).get("command_settled") or {}
        receipt = settled.get("receipt") or {}
        if (
            receipt.get("command") == command
            and receipt.get("actor") == ACTOR
        ):
            found.append(entry)
    return found


def settled_receipt(url, command):
    """The leg's admission out of the owner's live receipt mirror —
    the record matching (command, actor) once its outcome is
    terminal, None while it still reads `accepted` or the mirror
    does not answer."""
    try:
        receipts = simulate.http(f"{url}/receipts")
    except Exception:
        return None
    if not isinstance(receipts, list):
        return None
    for entry in receipts:
        if not isinstance(entry, dict):
            continue
        try:
            outcome = simulate.receipt_outcome(entry)
        except (KeyError, TypeError):
            continue
        if (
            entry.get("actor") == ACTOR
            and entry.get("command") == command
            and outcome != "accepted"
        ):
            return entry
    return None


def scan(url):
    """One driven `POST /scan`, tolerant — a dropped cycle is the
    window's or the restore's own evidence, never an unwind."""
    try:
        return simulate.http(f"{url}/scan", {"scans": 1})
    except Exception:
        return None


def realign_tick_order_pass(args, tamper):
    """The realign run: converge the declared pair, admit the
    receipted write, hold the frozen-source window with the standby
    scanning degraded, thaw and realign on resumed pulls, then audit
    the peer's durable journal — the tick axis non-regressing, the
    adopted settle journaled exactly once, the launch roles
    restored. Returns `(digest_entries, evidence, failures)`; raises
    `Inconclusive` where the pinned release predates the contract or
    the harness admits no lever for the staging."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "tracker-realign-tick-order leg has nothing to exercise"
        )
    with open(args.model) as handle:
        model = json.load(handle)
    command_point = writable_bool_point(model)
    if command_point is None:
        raise Abort(
            "the emitted model declares no writable boolean In point "
            "— the leg's receipted admission has nothing to land on"
        )
    mark_point = journaled_bool_point(model, exclude={command_point})
    if mark_point is None:
        raise Inconclusive(
            "the emitted model declares no journaled boolean input — "
            "the degraded window's transition evidence has no "
            "declared surface to journal through"
        )

    digest_entries, evidence, failures = [], {}, []
    inconclusive = []
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        journal_file = rig.standby_files.get("journal_file")
        if journal_file is None:
            raise Inconclusive(
                "the manifest's standby declares no journal_file — "
                "the durable half of the tick-axis audit is absent"
            )

        # Phase 1 — convergence: the manifest-declared pair settled on
        # its launch roles, the field owner active and the standby
        # tracking it off the configured wiring.
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

        # Phase 2 — the contract surface: the standby's durable
        # journal must exist and parse as the attributed durable
        # record the #830 clause audits — an absent or unreadable
        # journal is the pre-contract shape: inconclusive, never a
        # violation. Whether the records carry the integer tick axis
        # the contract pins is judged at the audit itself, where the
        # run's gained tail definitely holds records.
        if not os.path.isfile(journal_file):
            raise Inconclusive(
                "the standby's declared journal file does not exist "
                "— the pinned release predates the durable-record "
                "contract the leg audits"
            )
        try:
            baseline_entries = run_entries(journal_file)
        except Exception as error:
            raise Inconclusive(
                "the standby's durable journal could not be read: "
                f"{clean_error(error, rig)}"
            )
        floor = len(baseline_entries)
        evidence["baseline"] = role_row(standby_url)

        # Phase 3 — the induction admission: a receipted
        # `write_value` on the field owner whose settle the covering
        # checkpoint carries to the realigning peer — the
        # carried-apply-tick record the defect stamped below the
        # standing mark. A refused submission is staged around: the
        # window and the axis audit still run, and the staging
        # classification names the admission it never saw settle.
        command = {
            "write_value": {
                "point": command_point,
                "kind": "bool",
                "value": {"bool": True},
            }
        }
        admission = None
        try:
            status, receipt = pair.request(
                f"{duty_url}/command",
                {"command": command, "actor": ACTOR},
            )
            outcome = (
                simulate.receipt_outcome(receipt)
                if isinstance(receipt, dict)
                else None
            )
            admission = {"status": status, "outcome": outcome}
            if status != 200 or outcome != "accepted":
                inconclusive.append(
                    f"the induction command's submission answered "
                    f"{status} {receipt} — the settled receipt the "
                    "covering checkpoint must carry was never "
                    "admitted"
                )
        except Exception as error:
            inconclusive.append(
                f"the induction command's submission raised {error} "
                "— the settled receipt the covering checkpoint must "
                "carry was never admitted"
            )
        digest_entries.append({"phase": "admission", "admission": admission})

        # Phase 4 — the frozen-source window: the paused owner's
        # monitor socket stays bound but unanswered, so the peer's
        # per-scan checkpoint pulls produce nothing and its sync
        # verdict goes degraded — while its own scan clock, and the
        # journaled transition the injected fault lands, run ahead of
        # the frozen line.
        pause_peer(rig.duty)
        window = []
        degraded = None
        failover = None
        try:
            try:
                verdict = rig.plant_io.request(
                    {
                        "op": "inject_fault",
                        "point": mark_point,
                        "fault": {"quality": BAD_QUALITY},
                    }
                )
            except Exception as error:
                verdict = {"result": "error", "error": clean_error(error, rig)}
            if verdict.get("result") != "done":
                inconclusive.append(
                    f"inject_fault on point {mark_point} answered "
                    f"{verdict} — the window's transition evidence "
                    "could not stage"
                )
            for _ in range(WINDOW_SCANS):
                scan(standby_url)
                row = role_row(standby_url)
                if row is None:
                    continue
                window.append({"role": row["role"], "sync": row["sync"]})
                if row["role"] != "standby":
                    failover = row
                    break
                if row["sync"] == "degraded" and degraded is None:
                    degraded = row
        finally:
            resume_error = resume_peer(rig.duty)
        if resume_error is not None:
            inconclusive.append(
                "the frozen source was never thawed — the realign "
                f"could not stage: {resume_error}"
            )
        evidence["window"] = window
        if not window:
            inconclusive.append(
                "the held window collected no served rows — the "
                "starved monitor gave the audit nothing to read"
            )
        if failover is not None:
            inconclusive.append(
                "the tracking peer left its standby role inside the "
                "frozen-source window — the armed failover boundary "
                f"was reached inside the calibrated hold: {failover}"
            )
        if degraded is None:
            inconclusive.append(
                "the frozen source never produced the degraded "
                "verdict — the produced-nothing pulls the partition "
                "owes never landed"
            )
        digest_entries.append(
            {
                "phase": "window",
                "scans": len(window),
                "degraded": "seen" if degraded is not None else "none",
            }
        )

        # Phase 5 — the realign on resumed pulls: the owner's first
        # post-thaw scan applies the queued admission so the covering
        # checkpoint carries the settled receipt; the peer's
        # realigning apply lands at the run's own tick and adopts the
        # settle as the journaled `command_settled`.
        realigned = None
        for _ in range(REALIGN_SCANS):
            scan(duty_url)
            scan(standby_url)
            report = try_role(standby_url)
            if claim_reclaim.tracking(report):
                realigned = report
                break
        settled = settled_receipt(duty_url, command)
        if failover is None and realigned is None:
            failures.append(
                "the tracking peer never reconverged to tracking on "
                "the resumed pulls — the realign the axis audit is "
                f"staged across never landed: last row "
                f"{window[-1] if window else role_row(standby_url)}"
            )
        applied = (
            settled is not None
            and simulate.receipt_outcome(settled) == "applied"
        )
        apply_tick = (
            (settled.get("outcome") or {}).get("applied", {}).get("tick")
            if applied
            else None
        )
        if settled is None and admission is not None \
                and admission.get("outcome") == "accepted":
            inconclusive.append(
                "the induction command never settled applied on the "
                "field owner — the covering checkpoint carries no "
                "applied receipt to adopt"
            )
        elif settled is not None and not applied:
            inconclusive.append(
                "the induction command settled "
                f"{simulate.receipt_outcome(settled)} on the field "
                "owner, not applied — the covering checkpoint "
                "carries no applied receipt to adopt"
            )
        elif applied and apply_tick is None:
            inconclusive.append(
                "the induction command's applied receipt carries no "
                "line tick — the carried stamp the axis audit checks "
                "against is absent"
            )

        # The adopted settle's landing window: the carried receipt
        # the covering checkpoint must journal exactly once — a few
        # extra tracking-first scans cover a pull still riding the
        # window's stale in-flight fetch.
        entries = []
        journal_error = None
        try:
            entries = run_entries(journal_file)
        except Exception as error:
            journal_error = clean_error(error, rig)
        gained = entries[floor:]
        adopted = adopted_settles(gained, command)
        for _ in range(ADOPT_SCANS):
            if adopted or realigned is None:
                break
            scan(duty_url)
            scan(standby_url)
            try:
                entries = run_entries(journal_file)
            except Exception as error:
                journal_error = clean_error(error, rig)
                break
            gained = entries[floor:]
            adopted = adopted_settles(gained, command)

        # The recovery: the injected fault clears and its return
        # journals inside the audited axis — the mark's clearance
        # ordered after the adopted settle.
        if realigned is not None:
            try:
                verdict = rig.plant_io.request(
                    {"op": "clear_fault", "point": mark_point}
                )
                if verdict.get("result") != "done":
                    inconclusive.append(
                        f"clear_fault on point {mark_point} answered "
                        f"{verdict} — the recovery transition could "
                        "not stage"
                    )
            except Exception as error:
                inconclusive.append(
                    f"clear_fault on point {mark_point} raised "
                    f"{clean_error(error, rig)} — the recovery "
                    "transition could not stage"
                )
            for _ in range(RECOVERY_SCANS):
                scan(duty_url)
                scan(standby_url)

        try:
            entries = run_entries(journal_file)
            gained = entries[floor:]
            adopted = adopted_settles(gained, command)
        except Exception as error:
            journal_error = clean_error(error, rig)

        # Phase 6 — the axis audit: the peer's durable journal's
        # entry ticks across the run — every stamp at or after its
        # predecessor's, no record carrying a malformed axis. Under
        # `--tamper regressed-tick` the adopted settle's stamp is
        # doctored back to the carried line tick — the defect's own
        # rewind — and the audit must still name the regression.
        audit_entries = entries
        if tamper == "regressed-tick" and adopted:
            position = floor + gained.index(adopted[0])
            preceding = [
                entry["tick"]
                for entry in entries[:position]
                if isinstance(entry.get("tick"), int)
                and not isinstance(entry.get("tick"), bool)
            ]
            if preceding:
                mark = preceding[-1]
                stamp = (
                    min(apply_tick, mark - 1)
                    if isinstance(apply_tick, int)
                    else mark - 1
                )
                audit_entries = [dict(entry) for entry in entries]
                audit_entries[position]["tick"] = stamp
        axis = axis_audit(audit_entries)
        if journal_error is not None:
            inconclusive.append(
                "the peer's durable journal could not be read: "
                f"{journal_error}"
            )
        elif not axis["entries"]:
            inconclusive.append(
                "the standby's durable journal carries no tick-axis "
                "records across the realign — the staged run predates "
                "the attributed durable-record contract the leg "
                "audits"
            )
        else:
            if axis["malformed"]:
                failures.append(
                    f"the durable journal holds {axis['malformed']} "
                    "entry records carrying no integer tick — the "
                    "attributed-record axis the contract pins is "
                    "malformed"
                )
            if axis["regressions"]:
                first = axis["regressions"][0]
                failures.append(
                    "the standby's durable journal's tick axis "
                    f"regressed across the realign — seq "
                    f"{first['seq']} stamps tick {first['tick']} below "
                    f"the standing mark {first['below']}: "
                    f"{axis['regressions'][:4]}"
                )
            if failover is None and realigned is not None and applied:
                if len(adopted) != 1:
                    failures.append(
                        "the covering checkpoint's settled receipt "
                        f"journaled {len(adopted)} command_settled "
                        "records on the peer instead of exactly one "
                        "— the durable record the realign lands is "
                        + ("absent" if not adopted else "repeated")
                    )
                elif gained.index(adopted[0]) == 0:
                    inconclusive.append(
                        "the adopted settle leads the gained tail — "
                        "the degraded window journaled no transition "
                        "ahead of it, so the held-axis the audit "
                        "needs was never staged"
                    )
        digest_entries.append(
            {
                "phase": "axis",
                "axis": "ordered"
                if not axis["regressions"] and not axis["malformed"]
                and axis["entries"]
                else "unproven",
                "adopted": len(adopted),
            }
        )

        # Phase 7 — the launch roles restored: the field owner
        # `active`, the standby `tracking` behind it.
        duty_role = try_role(duty_url)
        standby_role = try_role(standby_url)
        restored = (
            (duty_role or {}).get("role") == "active"
            and claim_reclaim.tracking(standby_role)
        )
        if failover is None and not restored:
            failures.append(
                "the pair never settled back to its launch roles — "
                f"the field owner reports {duty_role}, the tracking "
                f"peer {standby_role}"
            )
        digest_entries.append(
            {
                "phase": "roles",
                "duty": (duty_role or {}).get("role"),
                "standby": "tracking"
                if claim_reclaim.tracking(standby_role)
                else claim_reclaim.sync_state(standby_role)
                if standby_role is not None
                else None,
            }
        )
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Inconclusive:
        raise
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        if rig is not None:
            if rig.duty is not None:
                resume_peer(rig.duty)
            rig.close()

    # The contract's clauses outrank the staging classifications: a
    # real journal-axis violation reports the named failure even when
    # the run also collected inconclusive evidence.
    if not failures and inconclusive:
        raise Inconclusive("; ".join(inconclusive))
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
        choices=["regressed-tick"],
        help="doctor the adopted settle's durable stamp back to the "
        "carried line tick — the axis audit must still report the "
        "regression",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = realign_tick_order_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                "realign-tick-order: the doctored stamp is the "
                "doctored case's proof — an inconclusive run offers "
                "it no evidence"
            )
            return 1
        eprint(f"realign-tick-order: inconclusive — {inconclusive}")
        print(f"realign-tick-order-digest inconclusive — {inconclusive}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"realign-tick-order: {line}")
        return 1
    for failure in failures:
        eprint(f"realign-tick-order: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"realign-tick-order: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored stamp"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"realign-tick-order-digest {digest} — tracking by tick "
        f"{evidence['converged']}, the frozen-source window held "
        f"{WINDOW_SCANS} degraded scans, the realign adopted the "
        "settled receipt once, and the durable journal's tick axis "
        "never regressed"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
