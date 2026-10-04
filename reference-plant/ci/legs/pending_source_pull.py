#!/usr/bin/env python3
"""The pending-source-pull latch leg for the reference plant — the
consumer-boundary mirror of the rig's pending-source-pull leg, pinning
#1315's transient checkpoint-pull recovery contract on the
manifest-declared deployment (WW-ENG-003, WW-LCM-001).

The platform pin is the fix's own regression — `dcs-monitor`'s
`a_pending_source_does_not_latch_the_standbys_tracking`: a standby's
checkpoint pulls against a live, serving source must converge once the
source serves, and one transient early failure must not latch past the
condition that produced it. The finding (`pending-source-pull-latches-
egain`, run `qax-20260929-005`, rig `lenovo`) recorded the latch: a
standby whose first pull met a still-pending source reported that one
refused read — `Resource temporarily unavailable (os error 11)` — as
its served `degraded` verdict on *every* cycle for the life of the
process, beside a healthy-looking peer, long after the same source
answered every later fetch. The cause was the puller's document bound:
a completed document was discarded once it was older than the fixed
one-second fetch bound standing in for the puller's cadence, so any
scan period past that bound made *every* document too old to apply and
the discard replayed the earliest fetch's failure instead. #1315
measures that wait against the puller's own cadence, clears the
preceding failure on any landed document, and reports a fetch that
outran its bound as the stall it is. Because the contract lives in the
checkpoint-pull path every tracking standby runs, the consumer boundary
pins it here — the file is the mirror of the rig's leg, named
`pending_source_pull.py` so the leg-discovered diagnostics land on this
issue's declared `pending-source-pull-*` stem.

The staging is the deployed deployment's own: the manifest-declared
pair settled and tracking, a labeled born-active seat that stands
pending against the frozen field, a labeled standby that begins
tracking inside that seat's pending window, and the thaw that lets the
source serve. The run:

- converges the manifest-declared pair in driven mode — the field
  owner `active`, the declared standby `tracking` it — and records the
  incumbent's owner token and both peers' journal positions, the
  baseline the episode must leave undisturbed;
- freezes the field: `SIGSTOP` on the spawned plant stand-in — the
  deploy stage's stop/pause lever beside `pair.stop`'s container stop
  — so no field contact is answered, the condition a born-active's
  deferred conditional startup grant stays pending under;
- stages a labeled born-active launch against the frozen field on the
  pair's own launch shape, declaring the pair's field owner under
  `--peer` so the run's post-thaw refusal rejoins the declared pair
  rather than ending the launch: it stands pending behind the honest
  standby surface — role `standby`, sync `unsynchronized`, no observed
  field claim, the startup log's pending report;
- holds the pending window with the seat's own stop lever — the
  `docker pause` shape the sibling freeze legs drive — so its monitor
  socket stays bound but never answers: the source that *accepts* the
  pull and produces nothing, the shape the finding recorded;
- launches the two labeled standbys inside that window on the
  deployment's paced shape — the harness's own labeled-launch lever
  paced (`--scan-ms`, no `--driven`, the pair's shared tracking token),
  which is what a deployed standby runs: the tracker names the pending
  seat in its `--standby` wiring — the manifest's `<name>:<port>` shape
  aimed at the pending seat's own address — so it begins tracking
  inside the window, while the control is the same shape aimed at the
  deployed active;
- reads the window through the labeled seats' served monitors: the
  tracker goes `degraded` naming the fetch failure its pending source
  answers with, the control — same shape, same pacing, a source that
  served throughout — converges `tracking`, and the deployed active's
  own served document answers throughout, so the two labeled runs
  differ only in their source's pending window;
- thaws the field and resumes the pending seat: its deferred grant
  meets the incumbent's standing claim, the declared-pair rejoin
  stands, and the tracked seat lands the source's document — the pull
  path's own recovery — inside the documented convergence bound, then
  settles on the *same* verdict its same-shape control holds inside
  the resolution bound (a pending source's own documents name no field
  owner, so the first landed verdict is the ownerless line's and the
  tracking-source resolution owes the deployed owner's line from
  there), and *stays* there across the settle hold: never a `degraded`
  verdict naming a failure the serving source has long since
  contradicted, and never a restart (the served journal's single
  cold-start lifetime beside a run tick that never rewound);
- restores the launch set: every labeled seat is gone, the deployed
  pair rests on its launch roles — the field owner `active`, the
  declared standby `tracking` it — with neither peer's journal gaining
  a disturbance record.

A pinned release predating the contract reports
`pending-source-pull-digest inconclusive`, never a failure: the
same-shape control's paced pulls cannot carry the deployed active's
served document across a cycle slower than the fixed one-second bound
the contract replaced — which is how every released artifact set reads
until the fix's release is cut, `docs/releases/v0.8.0`'s record
carrying it while its tag stands pending — a born-active launch
exiting at startup
naming a field-side refusal, a pending run serving no honest pending
surface, the labeled seats' served reports stalling inside the liveness
bound, the field owner's startup log carrying no owner-token claim
line, or the harness admitting no stop/pause lever.

Usage:

    pending_source_pull.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `pending-source-pull-digest <sha256>` line prints — the
check runs two passes and compares them
(`pending-source-pull-nondeterministic`). A contract violation reports
`pending-source-pull: …` lines on stderr and exits 1 — the check's
`pending-source-pull-failed`. `--tamper expect-latched-degraded`
doctors the convergence expectation to the defect shape — a standby
still `degraded` past the documented bound read as converged — so the
honest converging run must fail naming it.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import born_active_failure
import bounded_liveness
import claim_reclaim
import driver_recovery
import failover
import orphan_probe_cadence
import ownerless_backoff
import pair
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its two
# digest-identical passes, and exercise its doctored case. The doctored
# case: a leg reading a standby that stays degraded past the documented
# convergence bound as converged — the latch the contract closed — must
# surface the named diagnostic on the honest converging run, never let a
# defect pass silently.
LEG = {
    "order": 740,
    "title": "the pending-source-pull latch leg",
    "passes": "pending-source-pull",
    "tampers": [
        {
            "name": "expect-latched-degraded",
            "passed": "an expect-latched-degraded case passed the pending-source-pull leg",
            "missed": "the expect-latched-degraded case did not report its named diagnostic",
            "evidence": [
                "the doctored expectation wanted a latched degraded pull"
            ],
        },
    ],
}

def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort
sync_state = claim_reclaim.sync_state


class Inconclusive(Exception):
    """The pinned release predates the transient checkpoint-pull
    recovery contract — or the harness admits no lever for the staging —
    so the run classifies inconclusive, never a product failure.
    `args[0]` is the stable reason the digest line carries (two
    identical passes must share it); `args[-1]` the run's own evidence,
    reported on stderr only, where ephemeral addresses and measured
    windows belong."""


# The labeled seats' paced scan period — the deployment shape a real
# tracking standby runs: `--scan-ms` with no `--driven`, so the
# checkpoint-pull worker's cadence is the wall-clock period rather than
# an externally driven request. It is set above the one-second
# checkpoint-pull bound on purpose: that bound is the fixed
# standing-in-for-a-cadence the contract replaced, so the deployment
# shape it rejected is the one staged here, and the contract's own
# cadence-relative bound — twice the puller's longest poll gap — must
# carry the same document across the same cycle.
SCAN_MS = 1200

# The wall-clock bounds each phase polls. WINDOW_BOUND_S holds the
# pending window: the tracked seat's paced cycles each park in the
# frozen field's request timeout, so the window spans several of them
# and the leg asserts on the whole window rather than racing its first
# cycle. CONVERGE_BOUND_S is the documented convergence bound — once
# the field answers and the pending source serves, the tracked seat
# owes a landed document inside it; the pending window's own fetch
# failure must not outlive the window that produced it.
# RESOLVE_BOUND_S then carries the tracked seat off the pending
# source's ownerless line onto the deployed owner's — the
# tracking-source resolution's own bound, the tracked seat's first
# landed document naming a source that writes no field. SETTLE_S holds
# the settled verdict, the latch's own shape being a verdict that comes
# back. POLL_S paces the served-monitor reads and ANSWER_BOUND_S is the
# liveness bound each owed answer carries.
WINDOW_BOUND_S = 20.0
CONVERGE_BOUND_S = 30.0
RESOLVE_BOUND_S = 20.0
SETTLE_S = 8.0
POLL_S = 0.25
ANSWER_BOUND_S = 2.0

# The pending source's post-thaw settle — the driven scans that let its
# deferred grant meet the standing claim and rejoin the declared pair —
# and the restore ticks the deployed pair's launch roles are proved
# back on.
SOURCE_SETTLE_SCANS = 4
RESTORE_TICKS = 4

# The doctored case's named evidence — the defect expectation the
# tampered leg asserts, carried by both the doctored failure and the
# inconclusive-offers-no-evidence line so a predating release can never
# launder the unchecked self-check.
TAMPER_EVIDENCE = (
    "the doctored expectation wanted a latched degraded pull"
)


def converged_state(state):
    """Whether a served sync-state name is a landed document — the
    contract's own vocabulary, the state set
    `claim_reclaim.converged_sync` reads off a served RoleReport:
    `tracking` on a field owner's line, `orphaned` on an ownerless one.
    Either is a landed document; `degraded` never is."""
    return state in ("tracking", "orphaned")


def sync_detail(report):
    """The served `degraded` verdict's own detail — what the failed
    transfer named — or None on every other verdict."""
    sync = report.get("sync") if isinstance(report, dict) else None
    if isinstance(sync, dict) and "degraded" in sync:
        return (sync["degraded"] or {}).get("detail")
    return None


def pause_process(process, what):
    """The stop/pause lever on a labeled seat — `SIGSTOP`, the
    `docker pause` shape: the listener keeps accepting connections
    while nothing ever answers one, the connectable-but-silent source a
    pending window is. A lever the platform does not admit, or one that
    fails to land, classifies inconclusive."""
    if not hasattr(signal, "SIGSTOP"):
        raise Inconclusive(
            "the consumer harness admits no stop/pause lever — no "
            "SIGSTOP"
        )
    try:
        process.send_signal(signal.SIGSTOP)
    except Exception as error:
        raise Inconclusive(
            f"the stop/pause lever never landed on {what}",
            f"{error}",
        )


def resume_process(process):
    """The freeze's restore — `SIGCONT`, so the seat's next contact
    answers: the pause-then-unpause cycle the deployed `docker unpause`
    runs. A signal that cannot be delivered is not this leg's to
    report — the paused process is torn down either way."""
    try:
        process.send_signal(signal.SIGCONT)
    except Exception:
        pass


def spawn_paced_seat(args, plant_addr, standby, files):
    """Spawn one labeled standby on the deployment's paced shape —
    `dcs-controller <model> --remote <plant> --scan-ms <SCAN_MS>
    --listen … --standby <name> --pair-token <token>`: the paced run a
    deployed standby paces rather than the driven `POST /scan` run
    `pair.spawn_peer` builds, so the checkpoint-pull worker's cadence is
    the wall-clock period the contract measures its document bound
    against. `standby` is the tracked source's `<name>:<port>` wiring —
    the manifest's declared shape, aimed at the labeled seat's own
    address — and `files` the seat's runner-owned persistence under the
    rig's scratch. The per-scan snapshot stream goes to devnull (its
    line count is wall-clock data two identical passes cannot share)
    while stderr keeps draining off-thread past the `listening on`
    report. Returns `(process, monitor_url, preamble)` like the
    harness's own spawns — `monitor_url` None when the launch exits
    first, the preamble then carrying its startup refusal's lines."""
    argv = [
        args.controller,
        args.model,
        "--remote",
        plant_addr,
        "--scan-ms",
        str(SCAN_MS),
        "--listen",
        "127.0.0.1:0",
        "--dt",
        str(args.dt),
        "--pair-token",
        pair.PAIR_TOKEN,
    ]
    if standby is not None:
        argv += ["--standby", standby]
    for field, flag in (
        ("state_file", "--state-file"),
        ("journal_file", "--journal-file"),
    ):
        if files.get(field) is not None:
            argv += [flag, files[field]]
    process = subprocess.Popen(
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    preamble = []
    for line in process.stderr:
        line = line.strip()
        if "listening on" in line:
            orphan_probe_cadence.drain_stderr(process)
            return (
                process,
                "http://" + pair.dialable(line.rsplit(None, 1)[-1]),
                preamble,
            )
        preamble.append(line)
    process.wait(timeout=10)
    return process, None, preamble


def spawn_seat_or_classify(label, spawn):
    """One labeled launch plus the release gate: an exit naming the
    field-side refusal vocabulary is the pinned release predating the
    born-active pending substrate the window stages — inconclusive —
    while an exit naming nothing is a defect."""
    process, url, preamble = spawn()
    if url is not None:
        return process, url, preamble
    joined = " ".join(preamble)
    if any(word in joined for word in born_active_failure.PREDATING_WORDS):
        raise Inconclusive(
            "the pinned release predates the born-active pending "
            "substrate the pending window stages",
            f"{label} exited at startup naming a field-side refusal: "
            f"{'; '.join(preamble[-2:]) or 'no diagnostic'}",
        )
    raise Abort(
        f"{label} exited at startup without a field-side refusal: "
        f"{'; '.join(preamble) or 'no diagnostic'}"
    )


def served_role(url, label):
    """One bounded served-monitor read of a labeled seat's role report
    — the only surface the leg reads the pending window and its recovery
    from. A read stalling inside the liveness bound is the bounded
    substrate missing (inconclusive)."""
    report, seen = bounded_liveness.bounded_get(
        f"{url}/role", ANSWER_BOUND_S
    )
    if report is None:
        raise Inconclusive(
            "the pinned release predates the bounded-liveness "
            "substrate the leg reads its labeled seats through",
            f"{label}: GET /role answered {seen}",
        )
    return report


def poll_seats(urls, seconds):
    """Poll each `(label, url)` seat's served role report for
    `seconds`, returning each seat's verdict timeline — the
    `(tick, sync state, detail)` rows in poll order. The leg reads the
    timeline rather than one poll, so the evidence is a window's worth
    of served verdicts and never a race for a single cycle."""
    timelines = {label: [] for label, _url in urls}
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for label, url in urls:
            report, _seen = bounded_liveness.bounded_get(
                f"{url}/role", ANSWER_BOUND_S
            )
            if report is None:
                continue
            timelines[label].append(
                (report.get("tick"), sync_state(report), sync_detail(report))
            )
        time.sleep(POLL_S)
    return timelines


def await_state(url, label, accept, bound):
    """Poll a labeled seat's served sync-state name until `accept` takes
    it or `bound` seconds pass — the paced seat converges on its own
    cadence, so the leg waits on its served verdict rather than driving
    it. Answers the report that satisfied the predicate, else None."""
    deadline = time.monotonic() + bound
    while True:
        report = served_role(url, label)
        if accept(sync_state(report)):
            return report
        if time.monotonic() >= deadline:
            return None
        time.sleep(POLL_S)


def served_document(url):
    """The deployed active's own served checkpoint — the document the
    control's pulls land. Its tick when the endpoint answers inside the
    bound, else None: the evidence that the control's source served
    throughout, which is what separates a converged control from a
    pull path that carried no served document at all."""
    document, _seen = bounded_liveness.bounded_get(
        f"{url}/checkpoint", ANSWER_BOUND_S
    )
    if not isinstance(document, dict) or not isinstance(
        document.get("tick"), int
    ):
        return None
    return document.get("tick")


def restart_boundaries(url, failures):
    """The labeled seat's served `run_boundary` records — a new process
    lifetime journals one at bind over a journal file that already
    records an earlier lifetime. A cold-started seat's own first run
    needs no marker, so any marker names a restart: the contract's
    recovery is the run's own, never a fresh process's."""
    entries = pair.get(f"{url}/journal", "GET /journal", failures)
    return [
        entry
        for entry in entries
        if "run_boundary" in entry.get("event", {})
    ]


def settled(url, scans=SOURCE_SETTLE_SCANS):
    """The pending source's post-thaw settle: the driven scans that let
    its deferred grant meet the incumbent's standing claim and rejoin
    the declared pair. A scan the run has not finished answering is not
    this settle's failure to report — the rejoin is read off the
    source's own served verdict — so a transport error only ends the
    scan train. Answers the report the source stood `standby` tracking
    the incumbent on, else the last one read."""
    report = None
    for _ in range(scans):
        try:
            simulate.http(f"{url}/scan", {"scans": 1})
        except Exception:
            break
        report = served_role(url, "the pending seat")
        if claim_reclaim.tracking(report):
            break
        time.sleep(POLL_S)
    return report


def pending_source_pull_pass(args, tamper):
    """The pending-source-pull run: converge the deployed pair, freeze
    the field, stage the labeled born-active pending seat, hold the
    pending window with that seat's stop lever, launch the two labeled
    paced standbys inside the window, read the window through their
    served monitors, thaw, and assert the tracked seat converges on the
    same verdict its same-shape control holds — with no restart — then
    restore the launch set. Returns `(digest_entries, evidence,
    failures)`; raises `Inconclusive` where the pinned release predates
    the contract or the harness admits no lever."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "pending-source-pull leg has nothing to exercise"
        )
    _manifest, duty_decl, standby_decl = declared
    digest_entries, evidence, failures = [], {}, []
    rig = None
    seats = []
    paused = []
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        duty_addr = duty_url.removeprefix("http://")

        # Phase 1 — the deployed pair, settled and tracking, plus the
        # baseline the episode must leave untouched: the incumbent's
        # owner token, the substrate its post-thaw rejoin is arbitrated
        # against, and both peers' journal positions.
        converged = rig.converge(failures)
        evidence["converged"] = converged["ticks"][-1]
        if failover.owner_token(rig.duty_preamble) is None:
            raise Inconclusive(
                "the field owner's startup log carries no "
                "write-ownership claim line — the pinned release "
                "predates the startup-claim record the pending "
                "source's post-thaw rejoin stands on"
            )
        duty_journal0 = pair.get(
            f"{duty_url}/journal", "GET /journal", failures
        )
        standby_journal0 = pair.get(
            f"{standby_url}/journal", "GET /journal", failures
        )
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )

        # Phase 2 — the frozen field: the deploy stage's pause lever
        # holds the harness plant's process, so no field contact is ever
        # answered — the condition a born-active's deferred conditional
        # startup grant stays pending under.
        ownerless_backoff.pause_plant(rig)
        paused.append(rig.plant)

        # Phase 3 — the labeled born-active seat on the pair's own
        # launch shape, declaring the pair's field owner under `--peer`:
        # with no answered field contact it stands pending behind the
        # honest standby surface, and its post-thaw refusal rejoins the
        # declared pair rather than ending the launch.
        source, source_url, source_preamble = spawn_seat_or_classify(
            "the labeled born-active seat",
            lambda: born_active_failure.spawn_born_active(
                args,
                rig.plant_addr,
                born_active_failure.scratch_files(rig, "pending-source"),
                peer=duty_addr,
            ),
        )
        seats.append(source)
        report = served_role(source_url, "the pending seat")
        if not any("stands pending" in line for line in source_preamble):
            raise Inconclusive(
                "the pending seat's startup log carries no pending "
                "report — the pinned release predates the pending "
                "born-active substrate the window stages",
                f"{'; '.join(source_preamble[-2:]) or 'no diagnostic'}",
            )
        if (
            report.get("role") != "standby"
            or report.get("sync") != "unsynchronized"
            or report.get("field_claim") is not None
        ):
            raise Inconclusive(
                "the pending surface reads differently than the "
                "contract's recorded standby report — the pinned "
                "release predates the pending substrate the window "
                "stages",
                f"GET /role answers {report}",
            )
        source_addr = source_url.removeprefix("http://")
        digest_entries.append(
            {
                "phase": "pending-source",
                "surface": "standby/unsynchronized/no-claim",
                "peer": duty_decl["name"],
            }
        )

        # Phase 4 — the pending window's own stop lever: the labeled
        # seat's process is parked, so its monitor socket stays bound
        # but never answers — the source that accepts the pull and
        # produces nothing, the shape the finding recorded.
        pause_process(source, "the labeled born-active seat")
        paused.append(source)

        # Phase 5 — the two labeled standbys, launched inside that
        # window on the deployment's paced shape — the harness's own
        # labeled-launch lever paced, the sibling paced legs' shape:
        # the tracker names the pending seat in its `--standby` wiring,
        # so it begins tracking inside the window, while the control is
        # the same shape aimed at the deployed active — the
        # ordinary-cadence half of the contract, whose source never
        # stopped serving.
        tracker, tracker_url, _ = spawn_seat_or_classify(
            "the labeled tracking seat",
            lambda: spawn_paced_seat(
                args,
                rig.plant_addr,
                source_addr,
                born_active_failure.scratch_files(rig, "pending-tracker"),
            ),
        )
        seats.append(tracker)
        control, control_url, _ = spawn_seat_or_classify(
            "the labeled control seat",
            lambda: spawn_paced_seat(
                args,
                rig.plant_addr,
                duty_addr,
                born_active_failure.scratch_files(rig, "control"),
            ),
        )
        seats.append(control)

        # Phase 6 — the window, read through the labeled seats' served
        # monitors: the tracker reports the fetch failure its pending
        # source answers with, the control converges on the deployed
        # active, and the deployed active's own served document answers
        # throughout — so the two labeled runs differ only in their
        # source's pending window.
        timelines = poll_seats(
            (("tracker", tracker_url), ("control", control_url)),
            WINDOW_BOUND_S,
        )
        served = served_document(duty_url)
        tracker_rows = timelines["tracker"]
        control_rows = timelines["control"]
        degraded = [row for row in tracker_rows if row[1] == "degraded"]
        if not degraded:
            failures.append(
                "the tracked seat never reported a degraded verdict "
                "inside the pending window — its served verdicts "
                f"were {sorted({row[1] for row in tracker_rows})}: a "
                "source that accepts the pull and never answers owes "
                "the produced-nothing cycle the window is named for"
            )
            raise Abort
        if not any(source_addr in (row[2] or "") for row in degraded):
            failures.append(
                "the tracked seat's degraded verdict named no fetch "
                f"from the pending seat — it reported "
                f"{degraded[0][2]!r}, a failure the pending window "
                "must name for the leg to have staged it"
            )
            raise Abort
        if not any(row[1] == "tracking" for row in control_rows):
            # The control's source served throughout, so a control that
            # never converged did not lose its source: its paced pulls
            # could not carry the served document across a cycle
            # slower than the fixed fetch bound — the pre-contract
            # document bound, and the reason the pinned release
            # predates the contract.
            if served is not None:
                raise Inconclusive(
                    "the pinned release's paced checkpoint pull "
                    "carried no served document across a cycle slower "
                    "than its one-second fetch bound — the fixed "
                    "document bound #1315's cadence-relative staleness "
                    "replaced",
                    f"the control's served verdicts were "
                    f"{sorted({row[1] for row in control_rows})} while "
                    f"the deployed active served tick {served} "
                    "throughout",
                )
            failures.append(
                "the control seat never converged inside the pending "
                "window — its served verdicts were "
                f"{sorted({row[1] for row in control_rows})}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "pending-window",
                "tracker": "degraded",
                "detail": "names-pending-source",
                "control": "tracking",
                "source": "served" if served is not None else "silent",
            }
        )

        # Phase 7 — the thaw: the pending seat's deferred grant meets
        # the incumbent's standing claim, the declared-pair rejoin
        # stands, and the tracked seat's pull path takes the source's
        # document — the contract's own recovery — inside the documented
        # bound, never the pending window's failure latched beside a
        # healthy-looking peer.
        resume_process(source)
        paused.remove(source)
        ownerless_backoff.resume_plant(rig)
        paused.remove(rig.plant)
        tick_before = degraded[-1][0]
        rejoin = settled(source_url)
        if source.poll() is not None:
            failures.append(
                "the pending seat's launch ended instead of rejoining "
                "the declared pair once the field answered — a run "
                "whose pair was declared keeps standing"
            )
            raise Abort
        if not claim_reclaim.tracking(rejoin or {}):
            failures.append(
                "the pending seat never rejoined the declared pair "
                f"once the field answered — GET /role answers "
                f"{rejoin}: a run whose pair was declared stands and "
                "converges tracking on the incumbent"
            )
            raise Abort
        started = time.monotonic()
        tracker_report = await_state(
            tracker_url, "the tracked seat", converged_state, CONVERGE_BOUND_S
        )
        if tracker_report is None:
            report = served_role(tracker_url, "the tracked seat")
            named = sync_detail(report)
            failures.append(
                "the tracked seat never landed a document inside the "
                f"documented {CONVERGE_BOUND_S}s convergence bound — its "
                f"served verdict stayed {sync_state(report)!r}"
                + (f" naming {named!r}" if named else "")
                + " while the source served: the pending window's fetch "
                "failure latched past the condition that produced it"
            )
            raise Abort
        elapsed = round(time.monotonic() - started, 1)
        if tracker.poll() is not None:
            failures.append(
                "the tracked seat's process ended inside the recovery "
                "window — the contract's recovery is the run's own, "
                "never a relaunch's"
            )
            raise Abort
        restarts = restart_boundaries(tracker_url, failures)
        if restarts:
            failures.append(
                f"the tracked seat's served journal carries the run "
                f"boundaries {restarts} — a cold-started seat's own "
                "first run needs no marker, so the seat restarted "
                "rather than recovered"
            )
            raise Abort
        if not isinstance(tick_before, int) or not isinstance(
            tracker_report.get("tick"), int
        ) or tracker_report["tick"] < tick_before:
            failures.append(
                f"the tracked seat's run tick rewound across the thaw "
                f"— {tick_before} inside the pending window then "
                f"{tracker_report.get('tick')} after it: a restart's "
                "tick domain, not a recovery's"
            )
            raise Abort

        # The tracked seat's landed verdict names the line it landed
        # from: the pending source's own documents carry no field
        # ownership stamp — a run standing pending writes nothing — so
        # the first landed document reports the ownerless line's
        # `orphaned` verdict, and the tracking-source resolution owes
        # the deployed owner's line from there. Both labeled runs owe the
        # same verdict: the same-shape control's, `tracking`.
        control_report = served_role(control_url, "the control seat")
        if not claim_reclaim.tracking(control_report):
            failures.append(
                "the same-shape control seat does not report the "
                f"deployed active's line — GET /role answers "
                f"{control_report}"
            )
            raise Abort
        resolved = await_state(
            tracker_url,
            "the tracked seat",
            lambda state: state == "tracking",
            RESOLVE_BOUND_S,
        )
        if resolved is None:
            report = served_role(tracker_url, "the tracked seat")
            failures.append(
                "the tracked seat settled on the pending source's "
                f"ownerless line instead of resolving onto the deployed "
                f"owner's — its served verdict stayed "
                f"{sync_state(report)!r} where the same-shape control "
                f"reports {sync_state(control_report)!r}"
            )
            raise Abort

        # The settle hold: the latch's own shape is a verdict that comes
        # back, so the landed verdict must stand across the hold beside
        # the control's.
        held = poll_seats(
            (("tracker", tracker_url), ("control", control_url)), SETTLE_S
        )
        for key, label in (
            ("tracker", "the tracked seat"),
            ("control", "the control seat"),
        ):
            rows = held[key]
            if not rows or not all(row[1] == "tracking" for row in rows):
                failures.append(
                    f"{label}'s tracking verdict did not stand across "
                    f"the settle hold — it served "
                    f"{sorted({row[1] for row in rows})}: a verdict "
                    "that comes back is the pending window's failure "
                    "latched, or a source's line lost"
                )
                raise Abort
        if tamper == "expect-latched-degraded":
            # The doctored expectation — the defect shape: the
            # convergence the latch produced, a served verdict still
            # degraded past the documented bound. The honest run must
            # fail it.
            failures.append(
                f"{TAMPER_EVIDENCE} past the {CONVERGE_BOUND_S}s "
                f"convergence bound — the honest run landed a document "
                f"after {elapsed}s and held "
                f"{sync_state(resolved)!r} across the settle window"
            )
            raise Abort
        evidence["verdict"] = sync_state(resolved)
        digest_entries.append(
            {
                "phase": "recovery",
                "tracker": evidence["verdict"],
                "control": sync_state(control_report),
                "held": "converged",
                "restart": "none",
            }
        )

        # Phase 8 — the launch set restored: every labeled seat is gone
        # and the deployed pair rests on its launch roles, with neither
        # peer's journal gaining a disturbance record.
        for process in seats:
            pair.stop(process)
        seats.clear()
        owner = None
        for _ in range(RESTORE_TICKS):
            _tracked, owner = rig.tick(standby_url, duty_url, failures)
        if not driver_recovery.roles_hold(
            rig, failures, "after the pending-source-pull episode"
        ):
            raise Abort
        for peer_name, url, before in (
            (duty_decl["name"], duty_url, duty_journal0),
            (standby_decl["name"], standby_url, standby_journal0),
        ):
            leaked = [
                entry
                for entry in pair.get(
                    f"{url}/journal", "GET /journal", failures
                )[len(before):]
                if ownerless_backoff.DISTURBANCE_EVENTS
                & set(entry.get("event", {}))
            ]
            if leaked:
                failures.append(
                    f"{peer_name}'s journal gained disturbance records "
                    f"during the pending-source-pull episode: {leaked}"
                )
                raise Abort
        evidence["final_tick"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "restore",
                "duty": "active",
                "standby": "tracking",
                "journals": "undisturbed",
            }
        )
    except Inconclusive:
        raise
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        for process in paused:
            resume_process(process)
        for process in seats:
            pair.stop(process)
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
        choices=["expect-latched-degraded"],
        help="doctor the convergence expectation to the defect shape — "
        "a standby still degraded past the documented bound read as "
        "converged — so the pass must fail naming it",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = pending_source_pull_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                f"pending-source-pull: {TAMPER_EVIDENCE} — an "
                "inconclusive run offers the doctored case no evidence"
            )
            return 1
        reason = inconclusive.args[0]
        detail = inconclusive.args[-1]
        eprint(f"pending-source-pull: inconclusive — {detail}")
        print(
            f"pending-source-pull-digest inconclusive — {reason}"
        )
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"pending-source-pull: {line}")
        return 1
    for failure in failures:
        eprint(f"pending-source-pull: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"pending-source-pull: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored "
                "expectation"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"pending-source-pull-digest {digest} — the declared pair "
        f"converged by tick {evidence['converged']}, the tracked seat "
        "degraded on its pending source's refused pull and settled "
        f"{evidence['verdict']} on its control's line inside the "
        "documented convergence and resolution bounds without a "
        "restart, and the deployed pair's launch roles held at tick "
        f"{evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())