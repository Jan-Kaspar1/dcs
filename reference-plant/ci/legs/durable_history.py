#!/usr/bin/env python3
"""The durable process-history leg for the reference plant — the
consumer-boundary mirror of the qa lane's durable-history leg
(WW-ENG-003, WW-REP-001, WW-LCM-002): the released durable
process-history store exercised on the customer-owned redundant pair
the manifest declares, so a controller restart on the deployed pair —
not just the rig — resumes the store's recorded datasets with their
lifetime-attribution marks, and retention eviction surfaces to a
served consumer as a numbering gap rather than silent loss.

The restart legs (`ci/restart.py`, `ci/legs/standby_restart.py`) prove
the resumed run continues at the persisted tick and the durable
journal carries the run-boundary marker; the journal-boundary leg
(`ci/legs/journal_boundary.py`) proves journal eviction reads as a
served numbering gap. This leg pins the process-history half of the
same contract on the deployed pair through `GET /history/durable`:
each controller launches with the store's declared `--history-file`
beside its `--state-file`/`--journal-file` mounts — the manifest's
own persistence declarations instantiated at runner-owned scratch
paths — so the pair the leg drives is the manifest-declared one,
the store file included. The emitted model declares
no `record` duty, so the leg seeds the tracking peer's file with a
first lifetime's recorded datasets — the file's documented line
format, written at the leg's own seam — making the launched lifetime
the file's second run and giving the restart real datasets to
resume. The run:

- converges the manifest-declared pair to `tracking` through the pair
  rig's driven-tick loop, then reads the tracking peer's
  `GET /history/durable`: the seeded run-1 datasets verbatim ahead of
  the launched lifetime's `run_boundary` mark — and audits the field
  owner's own store, a single-lifetime file serving nothing yet;
- stops the tracking standby and relaunches it onto its declared
  `--state-file`/`--journal-file`/`--history-file` mounts — the
  manifest's own wiring —
  while the field owner keeps scanning through the downtime window;
  the relaunch must report the resume at the persisted tick, and the
  served durable stream must answer the recorded datasets verbatim
  behind the restart's `run_boundary` mark — the new lifetime's
  attribution — with the `seq` axis continuing the file's numbering;
- stops the peer once more and appends recorded datasets past the
  served window's retention bound at the file seam — the leg's
  fixture for a long accumulation — then relaunches: the served
  stream must read the pinned lifetime marks ahead of a retained tail
  whose `seq`s expose the evicted stretch as a numbering gap, while
  the file itself stays contiguous — eviction bounds the served
  window, never the record;
- drives the tracking-first ticks that rejoin the pair after each
  restart, then audits the launch roles restored: the field owner
  active, the declared standby tracking it — and the owner's store
  untouched by its peer's restarts.

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
import hashlib
import json
import os
import re
import subprocess
import sys
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


# The driven ticks each window runs — the downtime window the field
# owner keeps scanning through while its standby is dead, and the
# tracking-first window the relaunched peer re-tracks inside.
DOWNTIME_TICKS = 3
RECONVERGE_TICKS = 4

# The released store's served retention bound — `durable_capacity` is
# the tooling's fixed default, not a deployment knob — and the
# recorded datasets the leg's seeded first lifetime carries.
SERVED_BOUND = 1024
SEED_SAMPLES = 3


def history_records(path):
    """The `--history-file`'s lines in file order: `("boundary",
    {"run", "tick"})` markers and `("entry", entry)` records — the
    durable store's file format, audited at the leg's own seam like
    the journal file's."""
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


def sampled_entry(seq, point, value, tick):
    """One recorded `sampled` durable entry — the fixture dataset's
    wire form, identical served and filed."""
    return {
        "seq": seq,
        "tick": tick,
        "event": {
            "sampled": {
                "point": point,
                "sample": {
                    "value": {"float": value},
                    "quality": "good",
                    "tick": tick,
                },
            }
        },
    }


def seed_history(path, point):
    """Fixture the store's first lifetime: the run-1 `run_boundary`
    marker plus `SEED_SAMPLES` recorded `sampled` datasets on the
    emitted model's first declared point — the accumulation a restart
    must resume. Written in the file's documented record format while
    no process holds its writer lock."""
    with open(path, "w") as handle:
        handle.write(json.dumps({"run_boundary": {"run": 1, "tick": 0}}))
        handle.write("\n")
        for seq in range(1, SEED_SAMPLES + 1):
            entry = sampled_entry(seq, point, seq + 0.5, seq)
            handle.write(json.dumps({"entry": entry}))
            handle.write("\n")


def pad_history(path, first_seq, count, point):
    """Append `count` recorded `sampled` datasets continuing the
    file's `seq` axis — the leg's fixture driving the store past its
    served retention bound, written while the owning process is
    stopped so the single-writer lock never conflicts."""
    with open(path, "a") as handle:
        for seq in range(first_seq, first_seq + count):
            entry = sampled_entry(seq, point, float(seq), seq)
            handle.write(json.dumps({"entry": entry}))
            handle.write("\n")


def spawn_peer(binary, args, rig, files, standby=None):
    """Spawn one `dcs-controller --driven --remote` peer of the
    manifest-declared pair — `pair.spawn_peer`'s shape over the
    manifest's declared persistence fields, the durable store's
    `--history-file` included, instantiated under the rig's
    runner-owned scratch. Returns `(process, monitor_url, preamble)`:
    `monitor_url` is None when the process exits before reporting a
    listener, the preamble then carrying the startup refusal's stderr
    lines."""
    argv = [
        binary,
        args.model,
        "--remote",
        rig.plant_addr,
        "--driven",
        "--listen",
        "127.0.0.1:0",
        "--dt",
        str(args.dt),
    ]
    if standby is not None:
        argv += ["--standby", standby]
    argv += ["--pair-token", pair.PAIR_TOKEN]
    for field, flag in (
        ("state_file", "--state-file"),
        ("journal_file", "--journal-file"),
        ("history_file", "--history-file"),
    ):
        if files.get(field) is not None:
            argv += [flag, files[field]]
    process = subprocess.Popen(argv, stderr=subprocess.PIPE, text=True)
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
    """`spawn_peer` plus the failure classification: a controller
    refusing `--history-file` is a pinned release predating the store
    — inconclusive, never a startup failure."""
    process, url, preamble = spawn_peer(binary, args, rig, files, standby)
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


def durable_page(url, since, failures, doctor=None):
    """One `GET /history/durable?since=<since>` read — the durable
    store's served window, `seq`-cursor read like the journal's. A
    monitor answering the route's absence is a pinned release
    predating the store — inconclusive. `doctor`, when given,
    rewrites the page at the leg's own read seam — the tamper cases'
    doctored served answer, never a rig change."""
    try:
        body = simulate.http(f"{url}/history/durable?since={since}")
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
    return pair.get(f"{url}/role", "GET /role", failures)


def reconverge(rig, failures):
    """Tracking-first driven ticks until the resumed peer reports
    `tracking` inside the leg's declared window — the field owner's
    scans landing throughout. Returns the owner ticks."""
    ticks = []
    for _ in range(RECONVERGE_TICKS):
        _tracked, owner = pair.tick(
            rig.standby_url,
            rig.duty_url,
            failures,
            diverged="the resumed standby's image diverged from the "
            "field owner's at tick {tick} — the restart's re-pull "
            "never realigned the pair",
        )
        ticks.append(owner["tick"])
        if tracking(role(rig.standby_url, failures)):
            return ticks
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
    stopped = pair.get(
        f"{rig.standby_url}/snapshot", "GET /snapshot", failures
    )["tick"]
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


def relaunch(rig, args, failures, persisted):
    """Relaunch the stopped tracking peer on the manifest's wiring —
    its declared persistence files, the durable-history mount
    included — asserting the preamble reports the resume at the
    persisted tick and the resumed peer rejoins in standby, never
    claiming the field. Returns the startup preamble."""
    process, url, preamble = spawn_peer(
        args.controller,
        args,
        rig,
        rig.standby_files,
        standby=rig.duty_url.removeprefix("http://"),
    )
    rig.standby = process
    rig.standby_url = url
    if url is None:
        detail = "; ".join(preamble[-2:]) or "no diagnostic"
        failures.append(f"the relaunched standby exited at startup: {detail}")
        raise Abort
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
    standby_role = role(rig.standby_url, failures)
    if standby_role.get("role") != "standby" or (
        standby_role.get("sync") != "unsynchronized"
    ):
        failures.append(
            f"the resumed standby reports {standby_role} — expected "
            "standby/unsynchronized: it must rejoin in standby, "
            "never claiming the field"
        )
        raise Abort
    duty_role = role(rig.duty_url, failures)
    if duty_role.get("role") != "active":
        failures.append(
            f"the field owner reports {duty_role.get('role')!r} "
            "across the standby's relaunch, expected active — the "
            "resumed peer disturbed the field"
        )
        raise Abort
    return preamble


def declared_point(model_path, failures):
    """The emitted model's first declared io point's id — the point
    the leg's fixture datasets attribute to."""
    with open(model_path) as handle:
        model = json.load(handle)
    points = model.get("io_points") or []
    if not points:
        failures.append(
            "the emitted model declares no io points — the "
            "durable-history leg has no declared point to attribute "
            "its recorded datasets to"
        )
        raise Abort
    return points[0]["id"]


def durable_history_pass(args, tamper):
    """The durable-history run: seed the tracking peer's store, launch
    the pair on their declared wiring plus the store's file mount,
    converge, restart the peer onto its declared mounts, assert the
    recorded datasets resume behind the restart's run_boundary mark,
    then drive the file past the served retention bound and assert
    the eviction reads as a numbering gap — the pair restored to its
    launch roles throughout. Returns `(digest_entries, evidence,
    failures)`."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "durable-history leg has nothing to exercise"
        )
    digest_entries, evidence, failures = [], {}, []
    rig = pair.PairRig(declared)
    try:
        point = declared_point(args.model, failures)
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
        seed_history(history_file, point)

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

        # Phase 1 — convergence: the pair leg's driven-tick loop, the
        # tracking peer scanned first so each pull applies the
        # owner's latest checkpoint and the peers rest at the same
        # tick.
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

        # Phase 2 — the baseline: the tracking peer's served durable
        # stream carries the seeded run-1 datasets verbatim ahead of
        # the launched lifetime's `run_boundary` mark — the file's
        # second lifetime, its cold-start mark file-only like the
        # first's. The field owner's own store opened a single
        # lifetime and serves nothing until its first restart.
        base = durable_page(rig.standby_url, 0, failures)
        expected_base = [
            sampled_entry(seq, point, seq + 0.5, seq)
            for seq in range(1, SEED_SAMPLES + 1)
        ] + [
            {
                "seq": SEED_SAMPLES + 1,
                "tick": 0,
                "event": {"run_boundary": {"run": 2}},
            }
        ]
        if base != expected_base:
            failures.append(
                "the converged standby's served durable stream is "
                f"{base}, expected the seeded run-1 datasets verbatim "
                "behind the launched lifetime's run_boundary mark "
                f"{expected_base[-1]}"
            )
            raise Abort
        if durable_page(rig.duty_url, 0, failures) != []:
            failures.append(
                "the field owner's served durable stream answers "
                "entries on a single-lifetime store — cold starts "
                "carry no served mark"
            )
            raise Abort
        if history_records(rig.duty_files["history_file"]) != [
            ("boundary", {"run": 1, "tick": 0})
        ]:
            failures.append(
                "the field owner's durable file carries more than its "
                "cold-start run boundary — a peer's restarts reach "
                "into it"
            )
            raise Abort
        digest_entries.append(
            {"phase": "baseline", "served": base}
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
                != 3
            ]
        eviction_doctor = None
        if tamper == "hidden-eviction":
            eviction_doctor = lambda entries: [
                dict(entry, seq=index)
                for index, entry in enumerate(entries, 1)
            ]

        # Phase 3 — the restart: the tracking peer's declared state
        # file audited, its container stopped, the field owner kept
        # scanning through the downtime window, then the relaunch on
        # the manifest's own wiring — and the served stream answering
        # the recorded datasets verbatim behind the restart's
        # run_boundary mark, the seq axis continuing.
        persisted = stop_and_checkpoint(rig, failures)
        downtime = []
        for _ in range(DOWNTIME_TICKS):
            downtime.append(pair.scan(rig.duty_url, failures)["tick"])
        relaunch(rig, args, failures, persisted)
        resumed = durable_page(
            rig.standby_url, 0, failures, doctor=restart_doctor
        )
        expected_mark = {
            "seq": SEED_SAMPLES + 2,
            "tick": persisted,
            "event": {"run_boundary": {"run": 3}},
        }
        if resumed != expected_base + [expected_mark]:
            failures.append(
                "the relaunched peer's served durable stream does not "
                "answer the recorded datasets verbatim behind the "
                "restart's run_boundary mark "
                f"{expected_mark}: {resumed}"
            )
            raise Abort
        records = history_records(rig.standby_files["history_file"])
        marks = [record for kind, record in records if kind == "boundary"]
        if marks != [
            {"run": 1, "tick": 0},
            {"run": 2, "tick": 0},
            {"run": 3, "tick": persisted},
        ]:
            failures.append(
                f"the standby's durable file boundaries are {marks}, "
                "expected run 1 and 2 at tick 0 and run 3 at the "
                f"persisted tick {persisted} — the restarted lifetime "
                "is not attributed in the file's record"
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
        evidence["resumed_run"] = 3
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
        # throughout.
        ticks = reconverge(rig, failures)
        digest_entries.append({"phase": "reconverge", "ticks": ticks})

        # Phase 5 — retention: the peer stopped once more, its
        # durable file appended past the served window's bound at the
        # leg's own seam, then relaunched. The served stream must
        # read the pinned lifetime marks — the evicted boundary
        # entries — ahead of a retained tail whose seqs expose the
        # evicted stretch as a numbering gap, while the file itself
        # stays contiguous: eviction bounds the served window, never
        # the record.
        persisted2 = stop_and_checkpoint(rig, failures)
        history_file = rig.standby_files["history_file"]
        entries = [
            record
            for kind, record in history_records(history_file)
            if kind == "entry"
        ]
        first_seq = len(entries) + 1
        pad_history(
            history_file,
            first_seq,
            SERVED_BOUND + 8 - first_seq,
            point,
        )
        relaunch(rig, args, failures, persisted2)
        served = durable_page(
            rig.standby_url, 0, failures, doctor=eviction_doctor
        )
        records = history_records(history_file)
        filed = [record for kind, record in records if kind == "entry"]
        filed_seqs = [entry["seq"] for entry in filed]
        if filed_seqs != list(range(1, len(filed) + 1)):
            failures.append(
                "the durable file's seq axis is not contiguous — the "
                f"record itself lost entries: {filed_seqs[:8]}…"
            )
            raise Abort
        total = filed_seqs[-1]
        evicted = total - SERVED_BOUND
        if evicted <= 0:
            failures.append(
                f"the fixture's {total} recorded entries never "
                f"exceeded the served bound {SERVED_BOUND} — the "
                "retention check exercised nothing"
            )
            raise Abort
        pinned = [
            entry["seq"]
            for entry in filed
            if entry["seq"] <= evicted and marker(entry) is not None
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
        if served[-1] != {
            "seq": total,
            "tick": persisted2,
            "event": {"run_boundary": {"run": 4}},
        }:
            failures.append(
                "the second relaunch's run_boundary mark does not "
                f"head the retained tail: {served[-1]}"
            )
            raise Abort
        tail = durable_page(rig.standby_url, pinned[-1], failures)
        if tail != served[len(pinned) :]:
            failures.append(
                f"a since cursor at the last pinned mark answers "
                f"{[entry['seq'] for entry in tail][:6]}…, expected "
                "exactly the retained tail"
            )
            raise Abort
        evidence["evicted"] = evicted
        evidence["resumed_run4_tick"] = persisted2
        digest_entries.append(
            {
                "phase": "retention",
                "persisted": persisted2,
                "evicted": evicted,
                "pinned": pinned,
                "tail": [served_seqs[len(pinned)], served_seqs[-1]],
            }
        )

        # Phase 6 — the roles restored: the resumed peer re-tracks
        # inside the declared window and the pair rests in its launch
        # roles, the field owner active, the declared standby
        # tracking it — the owner's store untouched by its peer's
        # restarts.
        ticks = reconverge(rig, failures)
        if history_records(rig.duty_files["history_file"]) != [
            ("boundary", {"run": 1, "tick": 0})
        ]:
            failures.append(
                "the field owner's durable file moved across its "
                "peer's restarts — the store's record must stay "
                "per-process"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "restored",
                "ticks": ticks,
                "standby_role": role(rig.standby_url, failures),
                "duty_role": role(rig.duty_url, failures),
            }
        )
        evidence["final_tick"] = ticks[-1]
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
    digest = hashlib.sha256(
        json.dumps(digest_entries, sort_keys=True).encode()
    ).hexdigest()
    print(
        f"durable-history-digest {digest} — tracking by tick "
        f"{evidence['converged']}, run {evidence['resumed_run']} "
        f"resumed at tick {evidence['resumed_tick']}, run 4 resumed "
        f"at tick {evidence['resumed_run4_tick']} with "
        f"{evidence['evicted']} evicted entries reading as a "
        f"numbering gap, roles restored at tick "
        f"{evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
