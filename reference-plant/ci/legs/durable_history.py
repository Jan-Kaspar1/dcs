#!/usr/bin/env python3
"""The durable process-history leg for the reference plant — the
consumer-boundary mirror of the qa lane's durable-history leg
(WW-ENG-003, WW-REP-001, WW-LCM-002): the released durable
process-history store exercised on the customer-owned redundant pair
the manifest declares, against the plant's own declared recording
duties — the reportable series the emitted model's `record` fields
name — so a controller restart on the deployed pair resumes the
recorded datasets with their lifetime-attribution marks, and
retention eviction surfaces to a served consumer as a numbering gap
rather than silent loss.

The restart legs (`ci/restart.py`, `ci/legs/standby_restart.py`) prove
the resumed run continues at the persisted tick and the durable
journal carries the run-boundary marker; the journal-boundary leg
(`ci/legs/journal_boundary.py`) proves journal eviction reads as a
served numbering gap. This leg pins the process-history half of the
same contract on the deployed pair through `GET /history/durable`
and the `--history-file` the manifest declares per controller —
instantiated at runner-owned scratch paths, so the pair the leg
drives is the manifest-declared one, the store file included, and
every recorded dataset is a post-scan image sample the released
controller itself wrote at a declared duty's cadence. The run:

- converges the manifest-declared pair to `tracking` through the
  pair rig's driven-tick loop past two cadence intervals, then audits
  both peers' durable files and the tracking peer's served stream:
  the emitted model's declared `record` duties — the reportable
  series the composition names — account for every recorded sample,
  each point's stream lands at its declared cadence with the
  first-observation census, no undeclared point is ever recorded, and
  the tracking peer's recorded series is the field owner's own
  record, verbatim;
- stops the tracking standby and relaunches it onto its declared
  `--state-file`/`--journal-file`/`--history-file` mounts — the
  manifest's own wiring — after one field-owner downtime scan; the
  relaunch must report the resume at the persisted tick, the file's
  `run_boundary` record attributes the new lifetime, the served
  stream answers the recorded datasets verbatim behind the restart's
  `run_boundary` mark with the `seq` axis continuing, and the
  adopted cadence baselines hold: the resumed run's first post-
  boundary sample lands at the interval the file already paced out —
  not a fresh census at the resumed run's first scan;
- drives the field owner past the served window's retention bound
  with batched driven scans — real declared-duty samples only — and
  asserts the eviction reads as a numbering gap at the window's
  head while the file itself stays contiguous: eviction bounds the
  served window, never the record;
- drives the tracking-first ticks that rejoin the pair after the
  restart, then audits the launch roles restored — the field owner
  active, the declared standby tracking it — and the owner's store
  still a single lifetime, untouched by its peer's restart;
- launches one paced cold-start lifetime on the tracking peer's
  declared journal/history mounts — the `--scan-ms` deployment shape
  the rig definition paces at — so the durable file's own
  `run_boundary` record stamps the tick domain's minted civil-time
  anchor and the tracked line's adoption lands its `domain` seam:
  the boundary/anchor marks the file format declares, minted by the
  released binary rather than fixtured. Driven legs stay unanchored
  on purpose — their artifacts must stay byte-identical — so the
  anchor marks' exercise is this one paced lifetime, whose
  wall-clock anchor value never enters the digest.

The leg runs against the pinned release's tooling; a pin predating
the durable store — a controller refusing `--history-file`, a monitor
serving no `/history/durable` — reports `inconclusive`, never a
failure.

Usage:

    durable_history.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `durable-history-digest <sha256>` line prints — the
check runs two passes and compares them
(`durable-history-nondeterministic`). A contract violation reports
`durable-history: …` lines on stderr and exits 1 — the check's
`durable-history-failed`. `--tamper dropped-boundary` strips the
restart's `run_boundary` mark from the leg's read of the served
stream — the restarted lifetime's attribution never reaching the
consumer — and `--tamper hidden-eviction` renumbers the served seq
axis contiguously past the retention bound — the seamless record the
gap check must refuse; each doctored pass must exit nonzero carrying
its named evidence.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import pair
import simulate


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its
# two digest-identical passes, and exercise its doctored cases.
# The doctored cases: a served stream missing the restart's
# run_boundary mark, and a served seq axis renumbered gap-free past
# the retention bound, must each surface the named diagnostic —
# never a silently unattributed or silently-lossy pass.
LEG = {
    "order": 630,
    "title": "the durable process-history leg",
    "passes": "durable-history",
    "tampers": [
        {
            "name": "dropped-boundary",
            "passed": "a dropped-boundary case passed the durable process-history leg",
            "missed": "the dropped-boundary case did not report its named diagnostic",
            "evidence": ["the restart's run_boundary mark"],
        },
        {
            "name": "hidden-eviction",
            "passed": "a hidden-eviction case passed the durable process-history leg",
            "missed": "the hidden-eviction case did not report its named diagnostic",
            "evidence": ["numbering gap"],
        },
    ],
}


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The pinned release predates the durable process-history store —
    the run classifies inconclusive, never a product failure."""


# The driven ticks each phase runs — the converge long enough to
# bank two cadence intervals on every declared duty, the downtime
# window the field owner keeps scanning through while its standby is
# dead, and the tracking-first window the relaunched peer re-tracks
# inside.
CONVERGE_TICKS = 12
DOWNTIME_TICKS = 1
RECONVERGE_TICKS = 6

# The released store's served retention bound — `durable_capacity` is
# the tooling's fixed default, not a deployment knob — the bound the
# eviction phase rolls the field owner's real accumulation past.
SERVED_BOUND = 1024
# `POST /scan`'s per-request bound and the paced epilogue's real-time
# bounds — the wait for the paced lifetime's marks and samples, and
# its scan period.
SCAN_BATCH = 256
PACED_SCAN_MS = 10
PACED_DEADLINE = 10.0

# The reportable series the emitted model must declare `record`
# duties on — the customer-boundary pin for the issue's named
# series: both wet-well level measurements (10, 11), the station
# flow meters (12 inflow, 13 net flow), and each pump's totalized
# draw (20+i) and run feedback (40+i) — the declared point-id scheme
# the dynamics document addresses.
REPORTABLE_DUTIES = {10, 11, 12, 13, 20, 21, 40, 41}

# The event kinds a durable entry may carry — `sampled` datasets and
# the `run_boundary`/`domain` attribution marks; anything else is a
# malformed record.
ENTRY_KINDS = {"sampled", "run_boundary", "domain"}


def history_records(path):
    """The `--history-file`'s lines in file order: `("boundary",
    {"run", "tick", ...})` markers and `("entry", entry)` records —
    the durable store's file format, audited at the leg's own seam
    like the journal file's."""
    records = []
    with open(path) as handle:
        for number, line in enumerate(handle, 1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise Abort(
                    f"history file {path} line {number} does not parse: {error}"
                )
            if "run_boundary" in record:
                records.append(("boundary", record["run_boundary"]))
            elif "entry" in record:
                records.append(("entry", record["entry"]))
            else:
                raise Abort(
                    f"history file {path} line {number} is not a "
                    "history record"
                )
    return records


def declared_duties(model_path, failures):
    """The emitted model's declared `record` duties — `{point_id:
    {"every_ticks": ..., "retain_days": ...}}` read from the
    `io_points` section, every cadence asserted positive, and the
    issue's named reportable series asserted declared. An emitted
    model declaring none — the consumer boundary this leg exists to
    prove — fails the leg rather than fixture an answer."""
    with open(model_path) as handle:
        model = json.load(handle)
    duties = {}
    declared_ids = set()
    for point in model.get("io_points") or []:
        declared_ids.add(point["id"])
        record = point.get("record")
        if record is None:
            continue
        every = record.get("every_ticks")
        retain = record.get("retain_days")
        if not isinstance(every, int) or every <= 0:
            failures.append(
                f"io point {point['id']} declares a non-positive "
                f"recording cadence {every!r}"
            )
            raise Abort
        if retain is not None and (
            not isinstance(retain, int) or retain <= 0
        ):
            failures.append(
                f"io point {point['id']} declares a non-positive "
                f"retention {retain!r}"
            )
            raise Abort
        duties[point["id"]] = {"every_ticks": every, "retain_days": retain}
    if not duties:
        failures.append(
            "the emitted model declares no record duty — the "
            "composition's reportable series carry no durable "
            "recording, the gap this leg exists to close"
        )
        raise Abort
    missing = REPORTABLE_DUTIES - duties.keys()
    if missing:
        failures.append(
            f"the emitted model's record duties miss the reportable "
            f"series {sorted(missing)} — the wet-well level, station "
            "inflow, and pump run/totalized records the operations "
            "record is built from"
        )
        raise Abort
    undeclared = declared_ids - duties.keys()
    return duties, undeclared


def audit_entries(entries, duties, label, failures):
    """Audit one `DurableEntry` stream — the file's `entry` records or
    the served `/history/durable` answer, the same entry shape at
    both seams — against the emitted model's declared duties. Every
    record must be a declared kind; every `sampled` record must name
    a declared-duty point, land no denser than the point's declared
    cadence within its tick domain, and carry a well-formed
    quality-stamped sample; a `run_boundary`/`domain` mark opening a
    different anchor's domain resets the cadence baseline exactly
    the way the recorder clears it on a domain crossing. Returns
    `(sampled, counts, census_ticks)`: the `sampled` entries, the
    per-point sample count, and each point's first-sample tick in
    the last tick domain."""
    sampled = []
    counts = {}
    census_ticks = {}
    anchor = None
    last = {}
    for entry in entries:
        event = entry.get("event")
        kind = next(iter(event)) if isinstance(event, dict) else None
        if kind not in ENTRY_KINDS:
            failures.append(
                f"{label}: a durable entry carries a malformed "
                f"event: {entry!r}"
            )
            raise Abort
        body = event[kind] or {}
        if kind in ("run_boundary", "domain"):
            if kind == "run_boundary" and not isinstance(
                body.get("run"), int
            ):
                failures.append(
                    f"{label}: a run_boundary entry carries no "
                    f"lifetime ordinal: {entry!r}"
                )
                raise Abort
            stamped = body.get("anchor")
            if stamped is not None and not (
                isinstance(stamped, dict)
                and isinstance(stamped.get("epoch_ms"), int)
                and stamped["epoch_ms"] > 0
            ):
                failures.append(
                    f"{label}: a {kind} mark carries a malformed "
                    f"anchor: {entry!r}"
                )
                raise Abort
            # A mark on a different anchor's domain clears the
            # cadence baseline — the recorder's own rule — so the
            # post-mark stretch re-censuses.
            if stamped != anchor:
                anchor = stamped
                last = {}
                census_ticks = {}
            continue
        point = body.get("point")
        sample = body.get("sample")
        if point not in duties:
            failures.append(
                f"{label}: an undeclared point {point!r} was "
                f"recorded — the durable store may only write the "
                f"model's declared record duties: {entry!r}"
            )
            raise Abort
        if not isinstance(sample, dict) or not (
            isinstance(sample.get("value"), dict)
            and isinstance(sample.get("quality"), str)
            and isinstance(sample.get("tick"), int)
        ):
            failures.append(
                f"{label}: a sampled record carries a malformed "
                f"sample: {entry!r}"
            )
            raise Abort
        previous = last.get(point)
        if previous is not None and (
            entry["tick"] - previous < duties[point]["every_ticks"]
        ):
            failures.append(
                f"{label}: point {point} recorded at tick "
                f"{entry['tick']}, {entry['tick'] - previous} ticks "
                f"after its previous sample — inside its declared "
                f"cadence of {duties[point]['every_ticks']}"
            )
            raise Abort
        last[point] = entry["tick"]
        census_ticks.setdefault(point, entry["tick"])
        counts[point] = counts.get(point, 0) + 1
        sampled.append(entry)
    return sampled, counts, census_ticks


def spawn_paced(binary, args, rig, files, standby):
    """Spawn the epilogue's paced tracking peer — the deployment
    shape the rig definition declares (`--scan-ms`), a deliberate
    cold start (no `--state-file`) so the run's own pacing mints the
    tick domain's civil-time anchor at its origin tick, still wired
    at the field owner and still appending to the declared
    `--journal-file`/`--history-file` mounts. Its served snapshots
    go to devnull — the real-time scan count is the leg's one
    nondeterministic stdout line count, and two passes must print
    identical output. Returns `(process, monitor_url, preamble)`."""
    argv = [
        binary,
        args.model,
        "--remote",
        rig.plant_addr,
        "--scan-ms",
        str(PACED_SCAN_MS),
        "--listen",
        "127.0.0.1:0",
        "--standby",
        standby,
        "--pair-token",
        pair.PAIR_TOKEN,
    ]
    for field, flag in (
        ("journal_file", "--journal-file"),
        ("history_file", "--history-file"),
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
            raw = line.rsplit(None, 1)[-1]
            return process, "http://" + pair.dialable(raw), preamble
        preamble.append(line)
    process.wait(timeout=10)
    return process, None, preamble


def spawn_or_abort(name, binary, args, rig, files, standby=None):
    """`pair.spawn_peer` plus the failure classification: a controller
    refusing `--history-file` is a pinned release predating the store
    — inconclusive, never a startup failure."""
    process, url, preamble = pair.spawn_peer(
        binary,
        args.model,
        args.dt,
        rig.plant_addr,
        standby,
        files,
        pair_token=pair.PAIR_TOKEN,
    )
    if url is not None:
        return process, url, preamble
    if any(
        "unknown option" in line and "--history-file" in line
        for line in preamble
    ):
        raise Inconclusive(
            "the pinned release's controller takes no --history-file — "
            "the durable process-history store reaches the deployed "
            "pair's tooling with the repin carrying it"
        )
    raise Abort(
        f"{name} exited at startup: "
        f"{'; '.join(preamble[-2:]) or 'no diagnostic'}"
    )


def resilient_http(url, body=None):
    """`simulate.http` resubmitted across a dropped request — the
    empty-body `500` a shed or dispatcher-teardown drop answers with,
    or the refused/reset connection of the listener's rebind window.
    Both signatures mean the request never ran, so the resubmit
    replays nothing; a handler-answered failure re-raises at once."""
    for attempt in range(pair.SCAN_DROP_ATTEMPTS):
        try:
            return simulate.http(url, body)
        except Exception as error:
            if (
                simulate.dropped_request(error)
                and attempt + 1 < pair.SCAN_DROP_ATTEMPTS
            ):
                time.sleep(pair.SCAN_DROP_WAIT)
                continue
            raise


def durable_page(url, since, failures, point=None, doctor=None):
    """One `GET /history/durable?since=<since>` read — the durable
    store's served window, `seq`-cursor read like the journal's with
    the `?point=` filter scoping sampled records to one declared
    point while the pinned marks answer through. A monitor answering
    the route's absence is a pinned release predating the store —
    inconclusive. `doctor`, when given, rewrites the page at the
    leg's own read seam — the tamper cases' doctored served answer,
    never a rig change."""
    query = f"{url}/history/durable?since={since}"
    if point is not None:
        query += f"&point={point}"
    try:
        body = resilient_http(query)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise Inconclusive(
                "the pinned release serves no /history/durable — the "
                "durable process-history store reaches the deployed "
                "pair's tooling with the repin carrying it"
            )
        failures.append(
            f"GET /history/durable on {url} answered {error.code}"
        )
        raise Abort
    except Exception as error:
        failures.append(f"GET /history/durable on {url} answered {error}")
        raise Abort
    if not isinstance(body, list) or not all(
        isinstance(entry, dict)
        and isinstance(entry.get("seq"), int)
        and isinstance(entry.get("tick"), int)
        and isinstance(entry.get("event"), dict)
        for entry in body
    ):
        failures.append(
            "GET /history/durable answered a malformed page — the "
            f"rig predates the durable stream: {body!r}"
        )
        raise Abort
    if doctor is not None:
        body = doctor(body)
    return body


def marker(entry):
    """The `run_boundary`/`domain` mark an entry's event carries — the
    pinned attribution the retention bound evicts into the served
    stream's head rather than dropping — or None on an ordinary
    dataset."""
    event = entry.get("event") or {}
    for kind in ("run_boundary", "domain"):
        if kind in event:
            return kind
    return None


def tracking(report):
    """Whether a RoleReport reads `standby` under `tracking` sync."""
    sync = report.get("sync")
    return report.get("role") == "standby" and (
        isinstance(sync, dict) and "tracking" in sync
    )


def role(url, failures):
    """The peer's served RoleReport."""
    try:
        return resilient_http(f"{url}/role")
    except Exception as error:
        failures.append(f"GET /role answered {error}")
        raise Abort


def scan_n(url, scans, failures):
    """Drive `scans` scans through one `POST /scan` — the served
    batch bound paces the request; returns the served snapshot. A
    dropped request — the empty-body `500` a shed or dispatcher
    teardown answers with, or the refused connection of the
    listener's rebind window — never ran, so it resubmits inside the
    bounded window the monitor's rebind keeps; a handler-answered
    failure carries its named body and fails the leg on it."""
    for attempt in range(pair.SCAN_DROP_ATTEMPTS):
        try:
            return simulate.http(f"{url}/scan", {"scans": scans})
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace").strip()
            if (
                error.code == 500
                and not detail
                and attempt + 1 < pair.SCAN_DROP_ATTEMPTS
            ):
                time.sleep(pair.SCAN_DROP_WAIT)
                continue
            failures.append(
                f"POST /scan on {url} answered {error.code}: {detail}"
            )
            raise Abort
        except Exception as error:
            dropped = simulate.dropped_request(error)
            if dropped and attempt + 1 < pair.SCAN_DROP_ATTEMPTS:
                time.sleep(pair.SCAN_DROP_WAIT)
                continue
            failures.append(f"POST /scan on {url} answered {error}")
            raise Abort


def reconverge(rig, failures):
    """Tracking-first driven ticks until the resumed peer reports
    `tracking` inside the leg's declared window — the field owner's
    scans landing throughout. Returns `(tracked_ticks, ticks)`."""
    tracked_ticks = []
    ticks = []
    for _ in range(RECONVERGE_TICKS):
        tracked, owner = pair.tick(
            rig.standby_url,
            rig.duty_url,
            failures,
            diverged="the resumed standby's image diverged from the "
            "field owner's at tick {tick} — the restart's re-pull "
            "never realigned the pair",
        )
        tracked_ticks.append(tracked["tick"])
        ticks.append(owner["tick"])
        if tracking(role(rig.standby_url, failures)):
            return tracked_ticks, ticks
    failures.append(
        "the resumed standby never reconverged to tracking inside "
        f"the declared {RECONVERGE_TICKS}-tick window — GET /role "
        f"answers {role(rig.standby_url, failures)}"
    )
    raise Abort


def stop_and_checkpoint(rig, failures):
    """Stop the rig's tracking peer and audit the manifest-declared
    state file it leaves — the persisted tick equal to the tracking
    run's last served tick, under the manifest's fingerprint. Returns
    the persisted tick."""
    try:
        stopped = resilient_http(f"{rig.standby_url}/snapshot")["tick"]
    except Exception as error:
        failures.append(f"GET /snapshot answered {error}")
        raise Abort
    pair.stop(rig.standby)
    try:
        with open(rig.standby_files["state_file"]) as handle:
            checkpoint = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        failures.append(
            f"the standby's persisted state file does not parse: {error}"
        )
        raise Abort
    persisted = checkpoint.get("tick")
    if persisted != stopped:
        failures.append(
            f"the standby's state file persisted tick {persisted} "
            f"while the tracking run stood at {stopped}"
        )
        raise Abort
    if checkpoint.get("model_fingerprint") != rig.fingerprint:
        failures.append(
            f"the standby's state file carries fingerprint "
            f"{checkpoint.get('model_fingerprint')}, the manifest "
            f"declares {rig.fingerprint}"
        )
        raise Abort
    return persisted


def relaunch(rig, args, failures, persisted, files=None):
    """Relaunch the stopped tracking peer on the manifest's wiring —
    its declared persistence files, the durable-history mount
    included — asserting the preamble reports the resume at the
    persisted tick and the resumed peer rejoins in standby, never
    claiming the field. `files` overrides the relaunched mounts when
    a phase deliberately cold-starts the declared journal/history
    files. Returns the startup preamble."""
    process, url, preamble = pair.spawn_peer(
        args.controller,
        args.model,
        args.dt,
        rig.plant_addr,
        rig.duty_url.removeprefix("http://"),
        rig.standby_files if files is None else files,
        pair_token=pair.PAIR_TOKEN,
    )
    rig.standby = process
    rig.standby_url = url
    if url is None:
        detail = "; ".join(preamble[-2:]) or "no diagnostic"
        failures.append(f"the relaunched standby exited at startup: {detail}")
        raise Abort
    return preamble


def assert_resume(preamble, persisted, failures):
    """The relaunched peer's resume report: a startup naming the
    persisted tick, else the cold start silently abandoned the run."""
    line = next(
        (line for line in preamble if "resumed from state file" in line),
        None,
    )
    if line is None:
        failures.append(
            "the relaunched standby never reported a resume — its "
            "cold start at tick 0 silently abandons the persisted "
            f"run at tick {persisted}"
        )
        raise Abort
    match = re.search(r"at tick (\d+)", line)
    resumed = int(match.group(1)) if match else None
    if resumed != persisted:
        failures.append(
            f"the relaunch resumed at tick {resumed}, the state "
            f"file persisted {persisted}"
        )
        raise Abort


def durable_history_pass(args, tamper):
    """The durable-history run: converge the declared pair past two
    recording cadences, audit the declared-duty record both peers'
    stores carry, restart the tracking peer onto its declared mounts
    and pin the seq/boundary/cadence-baseline continuity, roll the
    owner's served window past its retention bound on real samples
    and pin the numbering gap, restore the launch roles, then mint
    the anchor marks on a paced cold-start lifetime. Returns
    `(digest_entries, evidence, failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "durable-history leg has nothing to exercise"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = pair.PairRig(declared)
    try:
        duties, undeclared = declared_duties(args.model, failures)
        state_file = rig.standby_files.get("state_file")
        journal_file = rig.standby_files.get("journal_file")
        history_file = rig.standby_files.get("history_file")
        if (
            state_file is None
            or journal_file is None
            or history_file is None
            or rig.duty_files.get("history_file") is None
        ):
            raise Abort(
                "the manifest's declared pair omits a "
                "state_file/journal_file/history_file — the "
                "durable-history leg exercises the manifest's own "
                "persistence declarations and has nothing to "
                "exercise"
            )

        rig.plant, rig.plant_addr = pair.spawn_plant(
            args.plant_server, args.model, args.dynamics
        )
        rig.duty, rig.duty_url, rig.duty_preamble = spawn_or_abort(
            f"the duty controller {rig.duty_decl['name']}",
            args.controller,
            args,
            rig,
            rig.duty_files,
        )
        rig.standby, rig.standby_url, rig.standby_preamble = (
            spawn_or_abort(
                f"the standby controller {rig.standby_decl['name']}",
                args.controller,
                args,
                rig,
                rig.standby_files,
                standby=rig.duty_url.removeprefix("http://"),
            )
        )

        # Phase 1 — convergence past two cadence intervals: the pair
        # leg's driven-tick loop, the tracking peer scanned first so
        # each pull applies the owner's latest checkpoint and the
        # peers rest at the same tick — each peer's durable store
        # accumulating its declared-duty samples all the while.
        converged = rig.converge(failures, count=CONVERGE_TICKS)
        evidence["converged"] = converged["ticks"][-1]
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "tracked_ticks": converged["tracked_ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )

        # Phase 2 — the baseline: both peers' durable files opened a
        # single lifetime and hold the declared-duty samples the
        # run's own scans wrote — the tracking peer's recorded series
        # the field owner's own record verbatim — and the tracking
        # peer's served stream answers its file's entries. The audit
        # pins the whole declared-duty contract at once: only
        # declared points recorded, each at its declared cadence with
        # the first-observation census, and the driven domain's marks
        # honestly unanchored.
        sb_records = history_records(rig.standby_files["history_file"])
        if [record for kind, record in sb_records if kind == "boundary"] != [
            {"run": 1, "tick": 0}
        ]:
            failures.append(
                "the tracking peer's durable file did not open a "
                "single cold-start lifetime — a peer's restarts "
                "reached into it before the leg ran"
            )
            raise Abort
        sb_entries = [
            record for kind, record in sb_records if kind == "entry"
        ]
        if [entry["seq"] for entry in sb_entries] != list(
            range(1, len(sb_entries) + 1)
        ):
            failures.append(
                "the tracking peer's durable file seqs are not "
                "contiguous: "
                f"{[entry['seq'] for entry in sb_entries][:8]}"
            )
            raise Abort
        _sampled, counts, census = audit_entries(
            sb_entries, duties, "the tracking peer's file", failures
        )
        for point, duty in duties.items():
            if counts.get(point, 0) < 2:
                failures.append(
                    f"declared-duty point {point} recorded "
                    f"{counts.get(point, 0)} samples inside "
                    f"{CONVERGE_TICKS} driven ticks — the declared "
                    f"cadence of {duty['every_ticks']} never paced a "
                    "second sample"
                )
                raise Abort
        if len(set(census.values())) != 1:
            failures.append(
                "the declared duties' first samples did not land on "
                f"one census tick: {census} — the standing "
                "first-observation census a new domain opens its "
                "record with"
            )
            raise Abort
        gaps = {}
        for point in duties:
            ticks = [
                entry["tick"]
                for entry in _sampled
                if entry["event"]["sampled"]["point"] == point
            ]
            gaps[point] = [b - a for a, b in zip(ticks, ticks[1:])]
        if any(
            gap != duty["every_ticks"]
            for point, duty in duties.items()
            for gap in gaps[point]
        ):
            failures.append(
                f"the converged run's sample intervals {gaps} do not "
                "hold each declared duty's declared cadence exactly"
            )
            raise Abort
        duty_records = history_records(rig.duty_files["history_file"])
        if [record for kind, record in duty_records if kind == "boundary"] != [
            {"run": 1, "tick": 0}
        ]:
            failures.append(
                "the field owner's durable file carries more than "
                "its cold-start run boundary"
            )
            raise Abort
        duty_entries = [
            record for kind, record in duty_records if kind == "entry"
        ]
        if duty_entries != sb_entries:
            failures.append(
                "the tracking peer's durable record is not the field "
                "owner's own series verbatim — the tracked image's "
                "recorded samples diverge from the owner's record"
            )
            raise Abort
        base = durable_page(rig.standby_url, 0, failures)
        if base != sb_entries:
            failures.append(
                "the converged standby's served durable stream does "
                "not answer its file's recorded datasets verbatim"
            )
            raise Abort
        duty_base = durable_page(rig.duty_url, 0, failures)
        if duty_base != duty_entries:
            failures.append(
                "the field owner's served durable stream does not "
                "answer its own file's recorded datasets verbatim"
            )
            raise Abort
        # The undeclared-point probe: a point the model declares but
        # records no duty for must answer an empty filtered series —
        # the filter's pinned marks being the only exceptions an
        # anchored era could mint.
        probe_point = sorted(undeclared)[0]
        filtered = durable_page(
            rig.standby_url, 0, failures, point=probe_point
        )
        if filtered != []:
            failures.append(
                f"a point-filtered read on undeclared point "
                f"{probe_point} answered {filtered} — an undeclared "
                "series entered the durable record"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "baseline",
                "duties": sorted(duties),
                "served": base,
                "duty_entries": duty_entries,
            }
        )

        # The tamper cases' doctored reads, applied at the leg's own
        # seam: `dropped-boundary` strips the restart's run_boundary
        # mark from the resumed stream — the restarted lifetime's
        # attribution never reaching the consumer — and
        # `hidden-eviction` renumbers the served seq axis
        # contiguously past the retention bound, the seamless record
        # the gap check must refuse.
        restart_doctor = None
        if tamper == "dropped-boundary":
            restart_doctor = lambda entries: [
                entry
                for entry in entries
                if (entry.get("event") or {}).get("run_boundary", {}).get(
                    "run"
                )
                != 2
            ]
        eviction_doctor = None
        if tamper == "hidden-eviction":
            eviction_doctor = lambda entries: [
                dict(entry, seq=index)
                for index, entry in enumerate(entries, 1)
            ]

        # Phase 3 — the restart: the tracking peer's declared state
        # file audited, its container stopped, one field-owner scan
        # through the downtime window, then the relaunch on the
        # manifest's own wiring — the served stream answering the
        # recorded datasets verbatim behind the restart's
        # run_boundary mark, the seq axis continuing, and the
        # adopted cadence baselines holding the run-1 intervals
        # rather than re-censusing at the resumed run's first scan.
        persisted = stop_and_checkpoint(rig, failures)
        downtime = [
            scan_n(rig.duty_url, 1, failures)["tick"]
            for _ in range(DOWNTIME_TICKS)
        ]
        preamble = relaunch(rig, args, failures, persisted)
        assert_resume(preamble, persisted, failures)
        resumed = durable_page(
            rig.standby_url, 0, failures, doctor=restart_doctor
        )
        expected_mark = {
            "seq": len(sb_entries) + 1,
            "tick": persisted,
            "event": {"run_boundary": {"run": 2}},
        }
        if resumed != sb_entries + [expected_mark]:
            failures.append(
                "the relaunched peer's served durable stream does "
                "not answer the recorded datasets verbatim behind "
                "the restart's run_boundary mark "
                f"{expected_mark}: {resumed}"
            )
            raise Abort
        # The resumed run's cadence baselines adopt the file's own
        # record: each declared point's next sample owes the
        # interval the run-1 record already paced out, so the first
        # post-boundary sample lands exactly one declared cadence
        # past the last pre-restart sample — a fresh baseline would
        # re-census at the resumed run's first scan instead. The
        # served stream holds no post-boundary samples yet — a
        # resumed driven run records nothing until it scans.
        last_pre = {
            entry["event"]["sampled"]["point"]: entry["tick"]
            for entry in sb_entries
            if "sampled" in entry.get("event", {})
        }
        records = history_records(rig.standby_files["history_file"])
        marks = [record for kind, record in records if kind == "boundary"]
        if marks != [
            {"run": 1, "tick": 0},
            {"run": 2, "tick": persisted},
        ]:
            failures.append(
                f"the standby's durable file boundaries are {marks}, "
                "expected run 1 at tick 0 and run 2 at the "
                f"persisted tick {persisted} — the restarted "
                "lifetime is not attributed in the file's record"
            )
            raise Abort
        entries = [record for kind, record in records if kind == "entry"]
        if [entry["seq"] for entry in entries] != list(
            range(1, len(entries) + 1)
        ):
            failures.append(
                "the standby's durable file seqs are not contiguous "
                f"across the restart: {[entry['seq'] for entry in entries]}"
            )
            raise Abort
        evidence["resumed_run"] = 2
        evidence["resumed_tick"] = persisted
        digest_entries.append(
            {
                "phase": "resume",
                "persisted": persisted,
                "downtime": downtime,
                "served": resumed,
            }
        )

        # Phase 4 — reconvergence: the resumed peer re-tracks inside
        # the declared window, the field owner's scans landing
        # throughout — and the adopted cadence baselines keep the
        # resumed run from re-censusing: each declared point's first
        # post-boundary sample lands exactly one declared cadence
        # past its last pre-restart sample. The loop drives past the
        # due tick — tracking answers inside a tick or two while the
        # baselines' next intervals land a few ticks later.
        tracked_ticks = []
        ticks = []
        entries = []
        expected_due = {
            point: tick + duties[point]["every_ticks"]
            for point, tick in last_pre.items()
        }
        for _ in range(RECONVERGE_TICKS):
            tracked, owner = pair.tick(
                rig.standby_url,
                rig.duty_url,
                failures,
                diverged="the resumed standby's image diverged from the "
                "field owner's at tick {tick} — the restart's re-pull "
                "never realigned the pair",
            )
            tracked_ticks.append(tracked["tick"])
            ticks.append(owner["tick"])
            entries = [
                record
                for kind, record in history_records(
                    rig.standby_files["history_file"]
                )
                if kind == "entry"
            ]
            post_boundary = [
                entry
                for entry in entries
                if entry["seq"] > len(sb_entries)
                and "sampled" in entry.get("event", {})
            ]
            covered = {
                entry["event"]["sampled"]["point"]
                for entry in post_boundary
            }
            if (
                tracking(role(rig.standby_url, failures))
                and covered >= duties.keys()
            ):
                break
        else:
            failures.append(
                "the resumed standby never re-tracked and re-"
                "recorded its declared duties inside the declared "
                f"{RECONVERGE_TICKS}-tick window — GET /role answers "
                f"{role(rig.standby_url, failures)}, post-boundary "
                f"samples cover {sorted(covered)}"
            )
            raise Abort
        for point in duties:
            ticks_p = [
                entry["tick"]
                for entry in post_boundary
                if entry["event"]["sampled"]["point"] == point
            ]
            if not ticks_p or ticks_p[0] != expected_due[point]:
                failures.append(
                    f"declared-duty point {point}'s first "
                    f"post-restart sample landed at tick "
                    f"{ticks_p[0] if ticks_p else None}, expected "
                    f"the adopted baseline's next interval "
                    f"{expected_due[point]} — the resumed run "
                    "re-censused instead of continuing the file's "
                    "own cadence"
                )
                raise Abort
        audit_entries(
            entries,
            duties,
            "the tracking peer's restarted file",
            failures,
        )
        digest_entries.append(
            {
                "phase": "reconverge",
                "ticks": ticks,
                "tracked_ticks": tracked_ticks,
                "post_boundary": post_boundary,
            }
        )

        # Phase 5 — retention: the field owner's own accumulation,
        # driven past the served window's bound by batched scans of
        # real declared-duty samples. The served stream's head rolls
        # forward — the evicted stretch reads as a numbering gap —
        # while the file itself stays contiguous: eviction bounds
        # the served window, never the record.
        filed = [
            record
            for kind, record in history_records(
                rig.duty_files["history_file"]
            )
            if kind == "entry"
        ]
        batches = 0
        while len(filed) <= SERVED_BOUND + 8:
            scan_n(rig.duty_url, SCAN_BATCH, failures)
            batches += 1
            filed = [
                record
                for kind, record in history_records(
                    rig.duty_files["history_file"]
                )
                if kind == "entry"
            ]
        filed_seqs = [entry["seq"] for entry in filed]
        if filed_seqs != list(range(1, len(filed) + 1)):
            failures.append(
                "the durable file's seq axis is not contiguous — "
                f"the record itself lost entries: {filed_seqs[:8]}…"
            )
            raise Abort
        total = filed_seqs[-1]
        evicted = total - SERVED_BOUND
        if evicted <= 0:
            failures.append(
                f"the run's {total} recorded entries never "
                f"exceeded the served bound {SERVED_BOUND} — the "
                "retention check exercised nothing"
            )
            raise Abort
        served = durable_page(
            rig.duty_url, 0, failures, doctor=eviction_doctor
        )
        pinned = [
            entry["seq"] for entry in filed[:evicted] if marker(entry)
        ]
        expected = pinned + list(range(evicted + 1, total + 1))
        served_seqs = [entry["seq"] for entry in served]
        if served_seqs != expected:
            failures.append(
                "the served durable stream's seq axis "
                f"{served_seqs[:6]}…{served_seqs[-3:]} does not "
                "surface the retention eviction's numbering gap — "
                f"expected the pinned marks {pinned} ahead of the "
                f"retained tail {evicted + 1}…{total}"
            )
            raise Abort
        tail = durable_page(rig.duty_url, evicted, failures)
        if tail != served[len(pinned) :]:
            failures.append(
                f"a since cursor at the eviction boundary answers "
                f"{[entry['seq'] for entry in tail][:6]}…, expected "
                "exactly the retained tail — fabricated continuity "
                "over the evicted stretch"
            )
            raise Abort
        audit_entries(
            filed, duties, "the field owner's accumulated file", failures
        )
        evidence["evicted"] = evicted
        digest_entries.append(
            {
                "phase": "retention",
                "batches": batches,
                "evicted": evicted,
                "pinned": pinned,
                "tail": [served_seqs[len(pinned)], served_seqs[-1]],
            }
        )

        # Phase 6 — the roles restored: the resumed peer re-tracks
        # inside the declared window and the pair rests in its
        # launch roles — the field owner's store still a single
        # lifetime, its peer's restarts and scan volume never
        # reaching into it.
        tracked_ticks, ticks = reconverge(rig, failures)
        if [
            record
            for kind, record in history_records(
                rig.duty_files["history_file"]
            )
            if kind == "boundary"
        ] != [{"run": 1, "tick": 0}]:
            failures.append(
                "the field owner's durable file carries a second "
                "lifetime — the store's record must stay "
                "per-process"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "restored",
                "ticks": ticks,
                "tracked_ticks": tracked_ticks,
                "standby_role": role(rig.standby_url, failures),
                "duty_role": role(rig.duty_url, failures),
            }
        )
        evidence["final_tick"] = ticks[-1]

        # Phase 7 — the anchor marks: a paced cold-start lifetime on
        # the tracking peer's declared journal/history mounts — the
        # deployment's `--scan-ms` shape, so the run's own pacing
        # mints the tick domain's civil-time anchor the file's
        # `run_boundary` record stamps, and the tracked line's
        # adoption lands the `domain` seam the crossing declares.
        # The wall-clock anchor value is the one nondeterministic
        # byte this leg can mint — it enters the evidence, never the
        # digest.
        pair.stop(rig.standby)
        paced_files = {
            "journal_file": rig.standby_files["journal_file"],
            "history_file": rig.standby_files["history_file"],
        }
        sb_total = len(
            [
                record
                for kind, record in history_records(
                    rig.standby_files["history_file"]
                )
                if kind == "entry"
            ]
        )
        paced, paced_url, paced_preamble = spawn_paced(
            args.controller,
            args,
            rig,
            paced_files,
            rig.duty_url.removeprefix("http://"),
        )
        if paced_url is None:
            detail = "; ".join(paced_preamble[-2:]) or "no diagnostic"
            failures.append(
                f"the paced standby exited at startup: {detail}"
            )
            raise Abort
        rig.standby = paced
        rig.standby_url = paced_url
        anchored = None
        domain_seam = None
        deadline = time.monotonic() + PACED_DEADLINE
        while time.monotonic() < deadline:
            try:
                records = [
                    record
                    for kind, record in history_records(
                        rig.standby_files["history_file"]
                    )
                    if kind == "boundary"
                ]
            except Abort:
                # A torn final line while the paced run appends —
                # retry on the next pass.
                records = []
            if records:
                last_boundary = records[-1]
                anchor = last_boundary.get("anchor")
                if last_boundary.get("run") == 3 and isinstance(
                    anchor, dict
                ) and isinstance(anchor.get("epoch_ms"), int):
                    try:
                        entries = [
                            record
                            for kind, record in history_records(
                                rig.standby_files["history_file"]
                            )
                            if kind == "entry"
                        ]
                    except Abort:
                        continue
                    domain = [
                        entry
                        for entry in entries
                        if "domain" in entry.get("event", {})
                    ]
                    paced_samples = [
                        entry
                        for entry in entries
                        if entry["seq"] > sb_total
                        and "sampled" in entry.get("event", {})
                    ]
                    if domain and len(paced_samples) >= len(duties):
                        anchored = last_boundary
                        domain_seam = domain[0]
                        break
            time.sleep(0.1)
        if anchored is None:
            failures.append(
                "the paced cold-start lifetime never stamped its "
                "minted anchor and domain seam into the declared "
                "history file — the run_boundary's anchor mark and "
                "the adoption's domain mark stayed unexercised"
            )
            raise Abort
        # The served form: the paced lifetime's run_boundary entry
        # carries the minted anchor to the consumer, and the domain
        # seam lands among the record's marks.
        served_marks = durable_page(paced_url, sb_total, failures)
        boundary_mark = next(
            (
                entry
                for entry in served_marks
                if (entry.get("event") or {}).get("run_boundary", {}).get(
                    "run"
                )
                == 3
            ),
            None,
        )
        if boundary_mark is None or (
            boundary_mark["event"]["run_boundary"].get("anchor")
            != anchored["anchor"]
        ):
            failures.append(
                "the paced lifetime's served run_boundary mark does "
                f"not carry the filed anchor {anchored['anchor']}: "
                f"{boundary_mark}"
            )
            raise Abort
        if domain_seam["event"]["domain"].get("anchor") is not None:
            failures.append(
                "the tracked adoption's domain seam carries an "
                "anchor the unanchored field owner's domain never "
                f"declared: {domain_seam}"
            )
            raise Abort
        evidence["anchored"] = True
        digest_entries.append(
            {
                "phase": "anchor",
                "boundary": {
                    "run": anchored["run"],
                    "tick": anchored["tick"],
                    "anchored": True,
                },
                "domain_seam": {
                    "seq": domain_seam["seq"],
                    "tick": domain_seam["tick"],
                    "anchored": False,
                },
            }
        )
    except Inconclusive:
        raise
    except Abort as abort:
        failures.extend(str(arg) for arg in abort.args)
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
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
        choices=["dropped-boundary", "hidden-eviction"],
        help="doctor the leg's own served read — the pass must fail "
        "naming the evidence",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = durable_history_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            doctored = {
                "dropped-boundary": "the doctored read dropped the "
                "restart's run_boundary mark",
                "hidden-eviction": "the doctored read renumbered the "
                "retention eviction's numbering gap away",
            }[args.tamper]
            eprint(
                f"durable-history: {doctored} — an inconclusive run "
                "offers the doctored case no evidence"
            )
            return 1
        eprint(f"durable-history: inconclusive — {inconclusive}")
        print(f"durable-history-digest inconclusive — {inconclusive}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"durable-history: {line}")
        return 1
    for failure in failures:
        eprint(f"durable-history: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"durable-history: the {args.tamper} case passed "
                "silently — the leg never noticed the doctored read"
            )
        return 1
    if failures:
        return 1
    digest = simulate.stable_digest(digest_entries)
    print(
        f"durable-history-digest {digest} — tracking by tick "
        f"{evidence['converged']}, run {evidence['resumed_run']} "
        f"resumed at tick {evidence['resumed_tick']} with adopted "
        f"cadence baselines, the field owner's window evicting "
        f"{evidence['evicted']} entries as a numbering gap, the "
        "paced lifetime's anchor and domain marks stamped, roles "
        f"restored at tick {evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
