#!/usr/bin/env python3
"""The history run-marker leg for the reference plant — the
consumer-boundary mirror of the qa lane's history-run-marker leg
(WW-ENG-003, WW-OPS-002): the served `PointHistory` `run` ordinal and
its tick-domain `seq` axis exercised on the customer-owned redundant
pair the manifest declares, so a controller restart on the deployed
pair — not just the rig — advances the served lifetime mark a
since-cursor consumer reads, instead of leaving it on a phantom-idle
stream.

The restart legs (`ci/restart.py`, `ci/legs/standby_restart.py`) prove
the resumed run continues at the persisted tick and the durable
journal carries the run-boundary marker; the journal-boundary leg
(`ci/legs/journal_boundary.py`) proves the marker reaches a
`GET /journal` consumer past the retained bound. This leg pins the
`GET /history` half of the same contract on the deployed pair: every
served `PointHistory` envelope stamps the producing process
lifetime's `run` ordinal — the same counter the journal file's
`run_boundary` markers carry — including on an answer whose `since`
filter empties `samples`, and the sample `seq`s ride the run's tick
domain so a `--state-file` resume keeps the axis continuous under the
new lifetime rather than restarting it silently. The run:

- converges the manifest-declared pair to `tracking` through the pair
  rig's driven-tick loop, then reads the tracking peer's
  `GET /history` for one served point accumulating samples — the held
  cursor the restart is watched across — every served envelope
  carrying the run-1 mark its journal file's boundary count shares;
- stops the tracking standby and relaunches it onto its declared
  `--state-file`/`--journal-file` mounts — the manifest's own wiring —
  while the field owner keeps scanning through the downtime window;
- holds the since-cursor across the restart: the relaunched peer's
  first answers — empty `samples` included — must already stamp the
  new lifetime's run 2, never the old mark's phantom-idle stream;
- drives the tracking-first ticks that rejoin the pair, then asserts
  the held cursor was never stranded: the cursor-filtered page
  carries every post-restart sample, a cursor parked past the axis's
  end still answers its stamped envelope, and the resumed run's full
  axis opens at or above the persisted tick — the tick domain a
  state-file resume restores;
- audits the record: the field owner's served run never moved — the
  mark is a per-process lifetime, not a line-wide counter — the
  standby's durable journal file carries the run-2 boundary at the
  persisted tick beside run 1's cold-start marker, and the pair rests
  in its launch roles, the standby tracking and the owner active.

Usage:

    history_run.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `history-run-digest <sha256>` line prints — the check
runs two passes and compares them (`history-run-nondeterministic`). A
contract violation reports `history-run: …` lines on stderr and exits
1 — the check's `history-run-failed`. `--tamper dropped-run` strips
the leg's read of the served envelope's `run` — a served envelope
that drops the restarted lifetime's mark — and `--tamper
rewound-axis` renumbers the resumed run's full-axis seqs from 1 —
the seamless restarted axis the tick-domain continuation check must
refuse; each doctored pass must exit nonzero carrying its named
evidence.
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


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored cases: a served envelope whose `run` never advances
# across the restart, and a restarted seq axis that reads as
# seamless, must each surface the named diagnostic — never a
# silently unattributed pass.
LEG = {
    "order": 280,
    "title": "the history run-marker leg",
    "passes": "history-run",
    "tampers": [
        {
            "name": "dropped-run",
            "passed": "a dropped-run case passed the history-run leg",
            "missed": "the dropped-run case did not report its named diagnostic",
            "evidence": ["no lifetime ordinal"],
        },
        {
            "name": "rewound-axis",
            "passed": "a rewound-axis case passed the history-run leg",
            "missed": "the rewound-axis case did not report its named diagnostic",
            "evidence": ["seq axis restarted"],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort

# The driven ticks each window runs — the downtime window the field
# owner keeps scanning through while its standby is dead, and the
# tracking-first window the relaunched peer re-tracks inside, the
# same scans that re-fill the watched point's run-2 axis.
DOWNTIME_TICKS = 3
RECONVERGE_TICKS = 4

# A `since` cursor parked beyond every seq the run will ever serve —
# the stranded reader whose answers must still carry the lifetime
# mark on an empty page.
FAR_SINCE = 1 << 40


def served_run(run):
    """The lifetime ordinal a served envelope must carry — a positive
    int, or None on a producer predating the marker contract."""
    if not isinstance(run, int) or isinstance(run, bool) or run < 1:
        return None
    return run


def axis_shape(seqs):
    """'empty' | 'ascending' | the violation — the served seq axis's
    ordering verdict."""
    if not seqs:
        return "empty"
    if any(not isinstance(seq, int) or isinstance(seq, bool) for seq in seqs):
        return "noninteger"
    if seqs != sorted(seqs) or len(set(seqs)) != len(seqs):
        return "disordered"
    return "ascending"


def history_page(url, point, since, failures, doctor=None):
    """One `GET /history?point=<point>&since=<since>` read: `(run,
    seqs)` for the watched point's served envelope — the lifetime mark
    and the seq axis the page answers. `doctor`, when given, rewrites
    the pair at the leg's own read seam — the tamper cases' doctored
    served answer, never a rig change."""
    body = pair.get(
        f"{url}/history?point={point}&since={since}",
        "GET /history",
        failures,
    )
    if not isinstance(body, list):
        failures.append(
            "GET /history answered a non-list payload — the rig "
            f"predates the history surface: {body!r}"
        )
        raise Abort
    for entry in body:
        if isinstance(entry, dict) and entry.get("point") == point:
            seqs = [row.get("seq") for row in entry.get("samples") or []]
            run = entry.get("run")
            if doctor is not None:
                run, seqs = doctor(run, seqs)
            return run, seqs
    failures.append(
        f"GET /history answered no envelope for the watched point {point}"
    )
    raise Abort


def tracking(report):
    """Whether a RoleReport reads `standby` under `tracking` sync."""
    sync = report.get("sync")
    return report.get("role") == "standby" and (
        isinstance(sync, dict) and "tracking" in sync
    )


def history_run_pass(args, tamper):
    """The history run-marker run: converge, baseline the held cursor,
    restart the tracking peer onto its declared files, then the
    run-marker, axis-continuity, stranded-cursor, sibling, journal,
    and role-restore audits. Returns `(digest_entries, evidence,
    failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "history-run leg has nothing to exercise"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = None
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        standby_files = rig.standby_files
        state_file = standby_files.get("state_file")
        journal_file = standby_files.get("journal_file")
        if state_file is None or journal_file is None:
            raise Abort(
                "the manifest's standby declares no "
                "state_file/journal_file — the history-run leg has "
                "nothing to exercise"
            )

        # Phase 1 — convergence: the pair leg's driven-tick loop, the
        # tracking peer scanned first so each pull applies the owner's
        # latest checkpoint and the peers rest at the same tick.
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

        # Phase 2 — the baseline: the tracking peer's full served
        # history. Every envelope must carry the lifetime ordinal —
        # the journal file's run count — and the watched point is the
        # first accumulating samples; the held cursor is its last
        # served seq.
        body = pair.get(
            f"{standby_url}/history?since=0", "GET /history", failures
        )
        if not isinstance(body, list):
            failures.append(
                "the tracking peer's /history answered a non-list "
                "payload — the rig predates the history surface"
            )
            raise Abort
        point = cursor = run0 = None
        for entry in body:
            if not isinstance(entry, dict):
                continue
            run = served_run(entry.get("run"))
            if run is None:
                failures.append(
                    "the tracking peer's served history envelope "
                    "carries no lifetime ordinal — the tooling "
                    "predates the run-marker contract: "
                    + json.dumps(entry)[:200]
                )
                raise Abort
            if point is None and entry.get("samples"):
                point = entry["point"]
                run0 = run
                seqs = [row.get("seq") for row in entry["samples"]]
                if axis_shape(seqs) != "ascending":
                    failures.append(
                        f"the watched point's baseline seq axis is "
                        f"{axis_shape(seqs)}: {seqs[:12]}"
                    )
                    raise Abort
                cursor = seqs[-1]
        if point is None:
            raise Abort(
                "the tracking peer serves no history point "
                "accumulating samples"
            )
        records = pair.journal_records(journal_file)
        marks0 = [record for kind, record in records if kind == "boundary"]
        if marks0 != [{"run": run0, "tick": 0}]:
            failures.append(
                f"the tracking peer serves run {run0} but its journal "
                f"file records boundaries {marks0} — the served marker "
                "is not the file's run count"
            )
            raise Abort
        duty_run0, _ = history_page(duty_url, point, 0, failures)
        digest_entries.append(
            {
                "phase": "baseline",
                "point": point,
                "run": run0,
                "cursor": cursor,
                "sibling_run": duty_run0,
            }
        )
        evidence["point"] = point
        evidence["cursor"] = cursor

        # The tamper cases' doctored reads, applied at the leg's own
        # seam: `dropped-run` strips every post-restart envelope's run
        # — the restarted lifetime's mark never reaching the held
        # cursor — and `rewound-axis` renumbers the resumed run's
        # full-axis seqs from 1, the seamless restarted axis the
        # continuation check must refuse.
        post_doctor = None
        if tamper == "dropped-run":
            post_doctor = lambda run, seqs: (None, seqs)
        axis_doctor = post_doctor
        if tamper == "rewound-axis":
            axis_doctor = lambda run, seqs: (
                run,
                list(range(1, len(seqs) + 1)),
            )
        expected = run0 + 1

        # Phase 3 — the restart: the tracking peer's last served tick
        # captured, its container stopped, its declared state file
        # audited for the persisted tick, and the field owner kept
        # scanning through the downtime window before the relaunch on
        # the manifest's own wiring.
        stopped = pair.get(
            f"{standby_url}/snapshot", "GET /snapshot", failures
        )
        pair.stop(rig.standby)
        try:
            with open(state_file) as handle:
                checkpoint = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            failures.append(
                f"the standby's persisted state file does not parse: {error}"
            )
            raise Abort
        persisted = checkpoint.get("tick")
        if persisted != stopped["tick"]:
            failures.append(
                f"the standby's state file persisted tick {persisted} "
                f"while the tracking run stood at {stopped['tick']}"
            )
            raise Abort
        if checkpoint.get("model_fingerprint") != rig.fingerprint:
            failures.append(
                f"the standby's state file carries fingerprint "
                f"{checkpoint.get('model_fingerprint')}, the manifest "
                f"declares {rig.fingerprint}"
            )
            raise Abort
        downtime = []
        for _ in range(DOWNTIME_TICKS):
            owner = pair.scan(duty_url, failures)
            downtime.append(owner["tick"])
        rig.standby, standby_url, preamble = pair.spawn_peer(
            args.controller,
            args.model,
            args.dt,
            rig.plant_addr,
            duty_url.removeprefix("http://"),
            standby_files,
            pair_token=pair.PAIR_TOKEN,
        )
        rig.standby_url = standby_url
        if standby_url is None:
            failures.append(
                "the relaunched standby exited at startup: "
                f"{'; '.join(preamble[-2:]) or 'no diagnostic'}"
            )
            raise Abort
        line = next(
            (line for line in preamble if "resumed from state file" in line),
            None,
        )
        if line is None:
            failures.append(
                "the relaunched standby never reported a resume — a "
                "cold start at tick 0 silently abandons the persisted "
                f"run at tick {persisted}"
            )
            raise Abort
        match = re.search(r"at tick (\d+)", line)
        resumed = int(match.group(1)) if match else None
        evidence["resumed"] = resumed
        if resumed != persisted:
            failures.append(
                f"the relaunch resumed at tick {resumed}, the state "
                f"file persisted {persisted}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "restart",
                "persisted": persisted,
                "resumed": resumed,
                "downtime": downtime,
            }
        )

        # Phase 4 — the held cursor across the gap: the relaunched
        # peer's first answers, before run 2's first driven scan, must
        # already stamp the new lifetime's mark — the volatile ring's
        # empty pages included — never the old mark's phantom-idle
        # stream.
        answers = []
        runs = set()
        stale_pages = []
        for since in (cursor, FAR_SINCE):
            run, seqs = history_page(
                standby_url, point, since, failures, doctor=post_doctor
            )
            answers.append({"since": since, "run": run, "samples": len(seqs)})
            runs.add(run)
            if seqs:
                stale_pages.append({"since": since, "head": seqs[:4]})
        if runs != {expected}:
            if len(runs) > 1:
                failures.append(
                    "the served run moved inside the resumed lifetime: "
                    + json.dumps(sorted(runs, key=lambda r: (r is None, r)))
                    + " — a flapping lifetime mark"
                )
            elif next(iter(runs)) is None:
                failures.append(
                    "the post-restart envelope carries no lifetime "
                    "ordinal — the restarted lifetime is unmarked"
                )
            else:
                failures.append(
                    f"the held since-cursor's served run stayed at "
                    f"{next(iter(runs))} — the restart's new lifetime "
                    f"{expected} never stamped the envelope: a "
                    "phantom-idle stream"
                )
            raise Abort
        if stale_pages:
            failures.append(
                "the relaunched peer's pre-scan pages answered "
                f"samples {stale_pages} — the volatile history ring "
                "was not fresh under the new lifetime"
            )
            raise Abort
        digest_entries.append({"phase": "held-cursor", "answers": answers})

        # Phase 5 — reconvergence: tracking-first driven ticks rejoin
        # the resumed peer to the owner's checkpoint stream — the same
        # scans that re-fill the watched point's run-2 axis — and the
        # pair's launch roles restore.
        ticks = []
        for _ in range(RECONVERGE_TICKS):
            pair.scan(standby_url, failures)
            owner = pair.scan(duty_url, failures)
            ticks.append(owner["tick"])
        standby_role = pair.get(f"{standby_url}/role", "GET /role", failures)
        duty_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        if not tracking(standby_role):
            failures.append(
                "the resumed standby never reconverged to tracking — "
                f"GET /role answers {standby_role}"
            )
            raise Abort
        if duty_role.get("role") != "active":
            failures.append(
                f"the field owner reports {duty_role.get('role')!r} "
                "after the standby's restart, expected active"
            )
            raise Abort
        evidence["final_tick"] = ticks[-1]
        digest_entries.append(
            {
                "phase": "reconverged",
                "ticks": ticks,
                "standby_role": standby_role,
                "duty_role": duty_role,
            }
        )

        # Phase 6 — the axis audits. The held cursor's page must carry
        # every post-restart sample — the cursor continued, never
        # stranded; the parked cursor's empty page must still stamp
        # the mark; and the full axis must open at or above the
        # persisted tick — the tick domain a state-file resume
        # restores, where a restarted axis opening near the floor is
        # the cold-start shape this leg forbids.
        run_c, seqs_c = history_page(
            standby_url, point, cursor, failures, doctor=post_doctor
        )
        if served_run(run_c) != expected:
            failures.append(
                f"the held cursor's continued page carries run "
                f"{run_c} — not the resumed lifetime {expected}"
            )
            raise Abort
        if not seqs_c:
            failures.append(
                f"the held cursor at seq {cursor} never saw a "
                "post-restart sample — the resumed axis left it "
                "stranded"
            )
            raise Abort
        if any(not isinstance(seq, int) or seq <= cursor for seq in seqs_c):
            failures.append(
                f"the held cursor's page carried samples at or below "
                f"seq {cursor}: {seqs_c[:12]} — the cursor semantics "
                "broke across the restart"
            )
            raise Abort
        run_f, _seqs_f = history_page(
            standby_url, point, FAR_SINCE, failures, doctor=post_doctor
        )
        if served_run(run_f) != expected:
            failures.append(
                f"the parked cursor's empty page carries run {run_f} "
                f"— the envelope dropped the mark on an empty answer"
            )
            raise Abort
        run_ax, seqs_ax = history_page(
            standby_url, point, 0, failures, doctor=axis_doctor
        )
        if served_run(run_ax) != expected:
            failures.append(
                f"the full history answer carries run {run_ax} — not "
                f"the resumed lifetime {expected}"
            )
            raise Abort
        shape = axis_shape(seqs_ax)
        if shape != "ascending":
            failures.append(
                f"the resumed run's seq axis is {shape}: {seqs_ax[:12]}"
            )
            raise Abort
        if seqs_ax[0] < persisted:
            failures.append(
                f"the resumed run's seq axis restarted at "
                f"{seqs_ax[0]} below the persisted tick {persisted} — "
                "a --state-file resume must continue the tick domain"
            )
            raise Abort
        evidence["axis_first"] = seqs_ax[0]
        digest_entries.append(
            {
                "phase": "axis",
                "run": run_ax,
                "first_seq": seqs_ax[0],
                "samples": len(seqs_ax),
                "cursor_samples": len(seqs_c),
            }
        )

        # Phase 7 — the record: the untouched peer's mark never moved
        # — `run` is per-process lifetime, never a line-wide counter —
        # and the standby's durable journal file holds one boundary
        # per lifetime, run 2's at the persisted tick.
        duty_run1, _ = history_page(duty_url, point, 0, failures)
        if duty_run1 != duty_run0:
            failures.append(
                "the untouched peer's served run moved during the "
                f"tracked peer's restart: {duty_run0} -> {duty_run1} — "
                "the mark is a per-process lifetime"
            )
            raise Abort
        records = pair.journal_records(journal_file)
        markers = [record for kind, record in records if kind == "boundary"]
        if markers != [
            {"run": run0, "tick": 0},
            {"run": expected, "tick": persisted},
        ]:
            failures.append(
                f"the standby's journal file boundaries are {markers}, "
                f"expected run {run0} at tick 0 and run {expected} at "
                f"the persisted tick {persisted} — the file's run "
                "count is the ordinal the envelope shares"
            )
            raise Abort
        records = pair.journal_records(rig.duty_files["journal_file"])
        duty_markers = [
            record for kind, record in records if kind == "boundary"
        ]
        if duty_markers != [{"run": duty_run0, "tick": 0}]:
            failures.append(
                f"the field owner's journal file boundaries are "
                f"{duty_markers}, expected its single cold-start "
                "marker — the untouched peer's file gained a lifetime"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "record",
                "sibling_run": duty_run1,
                "boundaries": markers,
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
    parser.add_argument("--plant-server", required=True)
    parser.add_argument("--controller", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--dynamics", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument(
        "--tamper",
        choices=["dropped-run", "rewound-axis"],
        help="doctor the leg's own read of the served answer — the "
        "pass must fail naming its evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = history_run_pass(
            args, args.tamper
        )
    except Abort as abort:
        for line in abort.args:
            eprint(f"history-run: {line}")
        return 1
    for failure in failures:
        eprint(f"history-run: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"history-run: the {args.tamper} case passed silently "
                "— the leg never noticed the doctored served answer"
            )
        return 1
    if failures:
        return 1
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"history-run-digest {digest} — point {evidence['point']} held "
        f"at seq {evidence['cursor']}: run advanced across the "
        f"standby's resume at tick {evidence['resumed']}, the run-2 "
        f"axis reopening at seq {evidence['axis_first']} and tracking "
        f"restored by tick {evidence['final_tick']} (converged at "
        f"{evidence['converged']})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
