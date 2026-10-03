#!/usr/bin/env python3
"""The incumbent-consultation leg for the reference plant — the
consumer-side proof that a relaunched active consults the live
incumbent before it takes the field's write-ownership claim: with a
reachable incumbent the startup claim refuses by its named verdict and
the incumbent's applied receipted state survives the restart intact,
while over a dead incumbent the restartee seizes with the takeover's
resumed baseline, consultation ledger, and grant verdict on the
release-served journal (WW-ENG-003, WW-LCM-001 — decision 98's
recorded incumbent-consultation precondition, the contract #735's
restart-as-active stale-checkpoint rollback fix lands on).

A duty container restarting while its promoted peer holds the field is
the case the unconditional startup claim cannot tell from the
documented dead-owner recovery: the restartee resumed its stale
`--state-file`, seized the claim, and silently reverted every
receipted tune, force, and carried state the incumbent accumulated
across the gap, journalling nothing about the takeover on the seizing
line. The released tooling evidences the defect and its fix on the
platform's own rig; nothing on a deployment a customer writes
evidenced either half, and the pair's own `deferred_startup_refusal`
leg needed a duplicate third launch rather than the declared pair's
own restart. The manifest-declared pair carries the honest shape: its
standby member's declared `--state-file`/`--journal-file` is exactly
the stale checkpoint a restarted container resumes, and its duty
member's declared claim is the live incumbent's.

The manifest declares no `--peer`, and the CLI's `--standby`/`--peer`
arguments name their tracking source as a *name* every pull
re-resolves — neither stamps an address into the persisted
checkpoint, so the consult's source is the `--peer` a relaunched
active declares. The leg therefore relaunches the demoted peer's
declared persistence with `--peer` naming the incumbent's monitor:
the documented invocation that both names the consult source and
gives the refusal its recorded disposition (a declared pair keeps the
run, rejoined as the incumbent's tracking standby). The run:

- converges the manifest-declared pair through the pair rig's
  driven-tick loop and gives the incumbent a receipted tune through
  `POST /command` — the descriptor-declared `sequencer` sequence
  parameter the tune-carryover leg tunes — asserting the `accepted`
  submission, the `applied` settlement in one adopted receipt log, and
  the tuned value live on both peers' served `parameters` reports and
  on the `out` port's bound point. The un-tuned served value is the
  control: it is what a restart that rolled the incumbent back would
  read, so the survival assertions below prove something;
- stops the demoted peer and drives the incumbent's own scans through
  the downtime window, so the incumbent's line stands strictly ahead
  of the persisted checkpoint the restartee resumes — the gap a
  stale-baseline seizure would roll back;
- half one, the refusal over the live incumbent: the demoted peer's
  declared persistence relaunched as an active with `--peer` naming
  the reachable incumbent. The consult runs ahead of the claim, and
  the leg replays the whole takeover record off the relaunch's served
  surfaces — the journaled `restart_consult` verdict and the resumed
  baseline it names, the named `startup_claim_refused` verdict, the
  `field_claim_observed` attribution to the incumbent's own recorded
  claim token, the rejoined run's `standby`/`unsynchronized` surface,
  the field's own arbitration still naming the incumbent's token, and
  the incumbent's receipted tune live on *both* served images under one
  adopted receipt log. Driven ticks then prove the refused launch is a
  working pair member again rather than a stray seat;
- half two, the seizure over the dead incumbent: the refused launch
  stops, the incumbent's container stops, and the same declared
  persistence relaunches with the dead incumbent still named as its
  `--peer`. The consult cannot be answered, the startup grant preempts
  the dead owner's standing claim, and the leg replays the takeover
  off the seizing run's served record — the `run_boundary` marker at
  the resumed baseline tick, the `restart_consult` naming the asked
  address and its `unadopted` verdict, the boundary ordered before it,
  no refusal or observed-claim entry beside it, the served role report
  answering `active` with the field claim `held`, the field's
  arbitration naming the restartee's own recorded token, and the
  incumbent's receipted tune still live on the recovered field's image;
- restores the manifest-declared pair's launch roles: the incumbent
  relaunches on its own declared persistence with `--standby` naming
  the current field owner — the remedy the refusal names — at its
  original monitor address, so the owner under restore's configured
  tracking name answers again, reconverges to `tracking` on it, and
  the documented demote/promote switch seats the declared duty member
  back as field owner with its declared standby tracking and the
  receipted tune intact across the switch.

The leg's expectation is stated once, in [`TAKEOVER`], and applied
nowhere else: `replay` is the single place the model's verdict is read
against a half's served facts, and the replay reads served facts only —
the released monitors' own `GET /role`, `GET /journal`, `GET
/checkpoint`, `GET /snapshot`, and `GET /receipts` answers beside the
field's own claim-arbitration probes. The durable record is read only
where the deployment's declared persistence *is* the subject: the
`--state-file` the restart resumes, whose persisted tick,
`generation`, and `command_admission` high-water are the resumed
baseline the takeover record must name.

Where the pinned release predates the contract the run reports
`incumbent-consultation-digest inconclusive` rather than asserting: a
relaunch that journals no `restart_consult` at all ran no consult — the
pre-#735 shape, where the restart reached the claim on its stale
persisted state alone — and the v0.10.0 artifact set this tree's
manifest pins is that predating release.

Usage:

    incumbent_consultation.py --plant-server PATH --controller PATH \
        --model model/plant.json --dynamics model/dynamics.json \
        --scenario ci/scenario.json --manifest deploy/manifest.json

On success one `incumbent-consultation-digest <sha256>` line prints —
the check runs two passes and compares them
(`incumbent-consultation-nondeterministic`). A contract violation
reports `incumbent-consultation: …` lines on stderr and exits 1 — the
check's `incumbent-consultation-failed`. `--tamper expect-seized`
doctors the leg's own expectation to the defect shape — asserting the
restart over a live incumbent seized the field and reverted the
incumbent's receipted state — so the leg proves its refusal
assertions fire rather than passing an unexercised contract.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import born_active_failure
import claim_reclaim
import failover
import pair
import simulate
import tune_carryover


# The leg's stage registration — ci/legs.py reads this literal
# (parsing, never importing the module) to order the leg, run its two
# digest-identical passes, and exercise its doctored case.
# The next free slot after the origin/main group added (unclaimed-rearm
# 820) — the stage runs the legs in this order and no two may share
# one. The doctored case: a leg asserting the restart over a live
# incumbent seized the field and reverted the incumbent's receipted
# state — the defect shape the contract closed — must surface the
# named diagnostic on the honest refusal rather than passing an
# unexercised contract.
LEG = {
    "order": 830,
    "title": "the incumbent-consultation leg",
    "passes": "incumbent-consultation-leg",
    "tampers": [
        {
            "name": "expect-seized",
            "passed": "an expect-seized case passed the incumbent-consultation leg",
            "missed": "the expect-seized case did not report its named diagnostic",
            "evidence": [
                "the doctored expectation wanted the live-incumbent restart to seize the field"
            ],
        },
    ],
}


def eprint(*args):
    print(*args, file=sys.stderr)


Abort = pair.Abort


class Inconclusive(Exception):
    """The pinned release predates — or never covers — the
    incumbent-consultation contract the leg exercises: carried as
    `(reason, detail)` — `reason` the stable phrase the inconclusive
    digest line prints (two passes must share it), `detail` the run's
    own verdicts, reported on stderr. A relaunch journalling no
    `restart_consult` ran no consult at all; a launched active exiting
    at startup on a field-side refusal predates the born-active
    contract's pending and rejoin dispositions."""


# The doctored case's declared evidence — the phrase ci/legs.py
# requires the tampered run's output to carry, and the one the honest
# run's own answered verdicts are reported against.
TAMPER_EVIDENCE = (
    "the doctored expectation wanted the live-incumbent restart to "
    "seize the field"
)


# The driven-scan bounds each phase gets: the downtime window the
# incumbent's line moves ahead of the restartee's persisted checkpoint,
# the reconvergence of the refused launch back onto the pair, the
# scans the seizing run takes over the field, and the restore's
# tracking-first ticks. The actor the leg's receipted submissions
# declare.
DOWNTIME_SCANS = 3
RECONVERGE_SCANS = 6
SEIZE_SCANS = 3
RESTORE_SCANS = 8
ACTOR = "ci-incumbent-consult"

# The receipted tune the incumbent is given — a finite value inside the
# descriptor's declared float range, distinct from the model's
# un-tuned default so a rolled-back restart reads differently.
TUNED = {"float": 2.5}


# The leg's model — the expectation each half's takeover record must
# meet, stated once here and applied nowhere else: `replay` is the
# single place the model is read against a half's served facts. Per
# half:
#
#   consult        the `RestartConsultOutcome` variant the journaled
#                  `restart_consult` must carry — `adopted` when a
#                  reachable, field-owning incumbent answered with a
#                  strictly newer line, `unadopted` when the consult
#                  could not be answered and the documented dead-owner
#                  recovery stands;
#   grant          the verdict that half's `grant_surface` must
#                  answer — the refused grant's own named refusal on
#                  the live-incumbent half, the field claim the
#                  granted run's served role report answers on the
#                  dead-incumbent one;
#   grant_surface  which of those two surfaces the `grant` word is
#                  read from, so the model names the record each
#                  half's verdict must land on rather than the caller;
#   baseline       which served surface carries the resumed baseline
#                  the takeover record must name — the adopted line's
#                  superseded tick where the consult carried the
#                  incumbent's state forward, the lifetime's
#                  `run_boundary` tick where no checkpoint was
#                  adopted;
#   holder         whose claim the field's own arbitration must still
#                  name once the restart has been answered;
#   carried        whether the incumbent's applied receipted state
#                  must read on the served images under one adopted
#                  receipt log — the runtime-tuning-continuity clause
#                  the stale-baseline seizure would have reverted.
TAKEOVER = {
    "refusal-over-live-incumbent": {
        "consult": "adopted",
        "grant_surface": "startup_claim_refused",
        "grant": "field_claim_failed",
        "baseline": "adopted.superseded_at",
        "holder": "incumbent",
        "carried": True,
    },
    "seizure-over-dead-incumbent": {
        "consult": "unadopted",
        "grant_surface": "role_field_claim",
        "grant": "held",
        "baseline": "run_boundary",
        "holder": "restartee",
        "carried": True,
    },
}


def consult_verdict(event):
    """The `RestartConsultOutcome` variant a journaled
    `restart_consult` entry carries — `adopted`, `standing`, or
    `unadopted` — or None where the entry is absent or carries no
    outcome this leg reads."""
    outcome = (event or {}).get("outcome")
    if not isinstance(outcome, dict):
        return None
    for verdict in ("adopted", "standing", "unadopted"):
        if verdict in outcome:
            return verdict
    return None


def grant_verdict(surface, served):
    """The grant verdict a half's named surface answers. The
    `startup_claim_refused` surface names the refused grant's own
    variant — `field_claim_failed` against a live incumbent — and the
    `role_field_claim` surface reads the field claim state the granted
    run's served role report answers once its first driven scan has
    asked the field. None on a surface the served record does not
    carry."""
    if surface == "startup_claim_refused":
        refusal = served.get("refusal")
        if not isinstance(refusal, dict):
            return None
        error = refusal.get("error")
        return next(iter(error)) if isinstance(error, dict) and error else None
    if surface == "role_field_claim":
        report = served.get("role")
        return report.get("field_claim") if isinstance(report, dict) else None
    return None


def baseline_from(surface, served):
    """The resumed baseline a half's named surface carries — the
    superseded tick inside an `adopted` consult outcome, or the
    lifetime's `run_boundary` entry tick."""
    if surface == "adopted.superseded_at":
        outcome = ((served.get("consult") or {}).get("outcome") or {}).get(
            "adopted"
        )
        return outcome.get("superseded_at") if isinstance(outcome, dict) else None
    if surface == "run_boundary":
        return (served.get("boundary") or {}).get("tick")
    return None


def holder_word(token, known):
    """Whose claim the field's arbitration named, as a
    digest-stable word — the recorded owner tokens are minted per
    process, so the verdict is named, never carried."""
    if token is None:
        return "unheld"
    if token in known:
        return known[token]
    return "foreign"


def sync_word(report):
    """The served RoleReport's sync verdict as one word — the named
    sync state's own variant key (`tracking`, `orphaned`,
    `degraded`) or the plain `unsynchronized` verdict a run reports
    before its first pull — and None where the report carries no sync
    section at all (an owning run)."""
    sync = report.get("sync") if isinstance(report, dict) else None
    if isinstance(sync, dict):
        return next(iter(sync), None)
    return sync


def latest_lifetime(entries):
    """The most recent process lifetime in a served journal — the
    `run_boundary` marker and the entries recorded behind it, the
    run's own record from its bind onward."""
    marker = None
    for index, entry in enumerate(entries):
        boundary = entry.get("event", {}).get("run_boundary")
        if boundary is not None:
            marker = {
                "run": boundary.get("run"),
                "tick": entry.get("tick"),
                "seq": entry.get("seq"),
                "index": index,
            }
    if marker is None:
        return {"run": None, "tick": None, "seq": None, "entries": []}
    return {**marker, "entries": entries[marker["index"] + 1:]}


def first_event(entries, name):
    """The first served entry carrying the named journal event."""
    for entry in entries:
        event = entry.get("event", {}).get(name)
        if event is not None:
            return event
    return None


def event_seq(entries, name):
    """The `seq` of the first served entry carrying the named journal
    event, or None where the lifetime recorded none."""
    for entry in entries:
        if name in entry.get("event", {}):
            return entry.get("seq")
    return None


def consulted(entries, half):
    """The lifetime's journaled `restart_consult` entry — the
    consultation ledger a takeover record must carry. A lifetime
    recording none ran no consult at all, which is the pre-contract
    shape rather than a defect: the restart reached the startup claim
    on its stale persisted state alone. Raises `Inconclusive` with the
    stable reason two passes share, and the lifetime's own tail as the
    detail reported on stderr."""
    consult = first_event(entries, "restart_consult")
    if consult is None:
        raise Inconclusive(
            "the pinned release predates the restart-as-active "
            "incumbent consult",
            f"the {half} lifetime journalled no restart_consult entry — "
            "the restart reached the startup claim with no "
            "consultation of any incumbent, the pre-contract shape: "
            f"{json.dumps(entries[-3:])[:400]}",
        )
    return consult


def resumed_tick(preamble):
    """The tick a launch's startup log reports resuming its declared
    `--state-file` at, or None where the launch reported no resume."""
    for line in preamble:
        if "resumed from state file" in line:
            found = re.search(r"at tick (\d+)", line)
            if found is not None:
                return int(found.group(1))
    return None


def persisted_checkpoint(path, failures):
    """The declared `--state-file`'s persisted checkpoint header — the
    launch's resumed baseline: the run `tick`, the stream `generation`
    the resume adopts, and the `command_admission` high-water the
    runtime's own continuity clause carries across the restart.
    Records a failure and unwinds where the file is absent or does
    not parse."""
    try:
        with open(path) as handle:
            checkpoint = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        failures.append(
            f"the declared --state-file {path} does not parse: {error}"
        )
        raise Abort
    return {
        "tick": checkpoint.get("tick"),
        "generation": checkpoint.get("generation"),
        "high_water": (checkpoint.get("command_admission") or {}).get(
            "high_water"
        ),
    }


def replay(half, served, failures):
    """Replay one half's served facts against the model's expectation
    for that half — the single place the expectation is applied, so
    neither phase restates it. `served` carries only what the
    released tooling served: `boundary` the lifetime's `run_boundary`
    marker, `consult` the lifetime's `restart_consult` entry,
    `refusal` its `startup_claim_refused` entry, `observed` the
    `field_claim_observed` attribution, `role` the served RoleReport,
    `holder` whose claim the field's arbitration names, `carried`
    whether the receipted state survived on the served images, and
    `persisted` the declared `--state-file`'s own baseline. The
    `observed` attribution the caller reads its own mark beside the
    model's — the refused grant's own record, not a second reading of
    the model. Returns the digest's marks for the half."""
    want = TAKEOVER[half]
    marks = {"consult": consult_verdict(served.get("consult"))}
    if marks["consult"] != want["consult"]:
        failures.append(
            f"the {half} consult journalled the "
            f"{marks['consult']!r} verdict — expected "
            f"{want['consult']!r}: {json.dumps(served.get('consult'))[:300]}"
        )
    marks["grant"] = grant_verdict(want["grant_surface"], served)
    if marks["grant"] != want["grant"]:
        failures.append(
            f"the {half} startup grant answered {marks['grant']!r} on "
            f"the {want['grant_surface']} surface — expected "
            f"{want['grant']!r}"
        )
    marks["baseline"] = baseline_from(want["baseline"], served)
    if marks["baseline"] != served.get("persisted"):
        failures.append(
            "the takeover record's resumed baseline reads "
            f"{marks['baseline']} on the {want['baseline']} surface — "
            "the declared --state-file persisted tick "
            f"{served.get('persisted')}: a restart that resumed a "
            "different state than the deployment persisted cannot name "
            "which baseline seized the field"
        )
    boundary_seq = (served.get("boundary") or {}).get("seq")
    consult_seq = event_seq(served.get("entries") or [], "restart_consult")
    marks["ordered"] = (
        boundary_seq is not None
        and consult_seq is not None
        and boundary_seq < consult_seq
    )
    if not marks["ordered"]:
        failures.append(
            f"the {half} takeover journalled its consultation ledger at "
            f"seq {consult_seq} against a run boundary at seq "
            f"{boundary_seq} — the consult is recorded behind the "
            "run-boundary marker and before the claim"
        )
    marks["holder"] = served.get("holder")
    if marks["holder"] != want["holder"]:
        failures.append(
            f"the field's arbitration names the {marks['holder']!r} "
            f"claim after the {half} restart — expected the "
            f"{want['holder']!r} claim to hold the field"
        )
    marks["carried"] = bool(served.get("carried"))
    if marks["carried"] != want["carried"]:
        failures.append(
            "the incumbent's applied receipted state did not survive "
            f"the {half} restart — expected carried="
            f"{want['carried']}: {served.get('carried_detail')}"
        )
    return marks


def resume_adoption(url, persisted, failures):
    """Whether the relaunched run's own served checkpoint continues the
    declared `--state-file`'s baseline — the resumed stream
    `generation` and the `command_admission` high-water it adopted, the
    two baseline facts the takeover record's name cannot be checked
    against otherwise. Returns the digest-stable word."""
    checkpoint = pair.get(f"{url}/checkpoint", "GET /checkpoint", failures)
    served_generation = checkpoint.get("generation")
    served_water = (checkpoint.get("command_admission") or {}).get(
        "high_water"
    )
    if served_generation != persisted["generation"]:
        failures.append(
            "the relaunched run's served checkpoint carries generation "
            f"{served_generation} against the declared --state-file's "
            f"{persisted['generation']} — a warm resume continues the "
            "persisted run's stream identity rather than opening a new "
            "tick domain"
        )
        return "opened"
    if served_water != persisted["high_water"]:
        failures.append(
            "the relaunched run's served checkpoint carries the "
            f"command-admission high-water {served_water} against the "
            f"declared --state-file's {persisted['high_water']} — the "
            "resumed baseline's admission window was not carried"
        )
        return "restarted"
    return "adopted"


def tune_carried(images, component, parameter, point, command, failures):
    """Whether every served image in `images` — `(url, what)` pairs —
    still carries the incumbent's receipted tune, and every image's
    adopted receipt log is the same one log. Returns
    `(carried, detail)`; the detail names the first fact that did not
    hold, for the replay's own single failure."""
    logs = []
    for url, which in images:
        report = pair.get(f"{url}/snapshot", "GET /snapshot", failures)
        served = tune_carryover.parameter_value(report, component, parameter)
        if served != TUNED:
            return False, (
                f"{which} parameter report reads {served} for "
                f"{component}'s {parameter}, expected {TUNED}"
            )
        sample = simulate.snapshot_point(report, point)
        if sample != TUNED:
            return False, (
                f"{which} out point {point} reads {sample}, expected "
                f"{TUNED}"
            )
        logs.append(
            (
                url,
                pair.get(f"{url}/receipts", "GET /receipts", failures),
            )
        )
    first = logs[0][1]
    for url, log in logs[1:]:
        if log != first:
            return False, (
                f"{url} and {logs[0][0]} serve divergent adopted "
                "receipt logs — the carried audit is not one log"
            )
    if not any(
        entry.get("command") == command
        and simulate.receipt_outcome(entry) == "applied"
        for entry in first
    ):
        return False, (
            "the adopted receipt log carries no applied settlement for "
            "the incumbent's receipted tune"
        )
    return True, None


def spawn_restartee(args, plant_addr, files, peer=None):
    """Spawn a relaunched active on the declared pair's launch shape —
    `dcs-controller <model> --remote <plant> --driven --pair-token`, the
    declared persistence paths under the rig's runner-owned scratch, and
    `peer` the `--peer` argument naming the incumbent whose checkpoint
    stream the startup consult pulls. Returns
    `(process, monitor_url, preamble)` like `pair.spawn_peer` —
    `monitor_url` None where the process exits before reporting a
    listener, the preamble then carrying the startup refusal's stderr
    lines."""
    argv = [
        args.controller,
        args.model,
        "--remote",
        plant_addr,
        "--driven",
        "--listen",
        "127.0.0.1:0",
        "--dt",
        str(args.dt),
        "--pair-token",
        pair.PAIR_TOKEN,
    ]
    if peer is not None:
        argv += ["--peer", peer]
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
            return (
                process,
                "http://" + pair.dialable(line.rsplit(None, 1)[-1]),
                preamble,
            )
        preamble.append(line)
    process.wait(timeout=10)
    return process, None, preamble


def relaunch(label, args, plant_addr, files, peer, relaunches):
    """The relaunch with the release gate a field-side startup exit
    carries: under the born-active contract a launched active never
    exits for a field-side startup condition, so an exit naming the
    field-side refusal vocabulary is the pinned release predating that
    contract — inconclusive — while an exit naming nothing is a defect.
    `relaunches` collects the spawned children the run's teardown
    stops."""
    process, url, preamble = spawn_restartee(
        args, plant_addr, files, peer=peer
    )
    relaunches.append(process)
    if url is not None:
        return process, url, preamble
    joined = " ".join(preamble)
    if any(word in joined for word in born_active_failure.PREDATING_WORDS):
        raise Inconclusive(
            "the pinned release predates the born-active startup "
            f"refusal disposition — {label} exited at startup naming a "
            f"field-side refusal: "
            f"{'; '.join(preamble[-2:]) or 'no diagnostic'}",
        )
    raise Abort(
        f"{label} exited at startup without a field-side refusal: "
        f"{'; '.join(preamble) or 'no diagnostic'}"
    )


def incumbent_consultation_pass(args, tamper):
    """The incumbent-consultation run: converge, arm the incumbent's
    receipted state, open the downtime window, the refusal over the
    live incumbent, the seizure over the dead one, and the restore.
    Returns `(digest_entries, evidence, failures)`; raises
    `Inconclusive` where the pinned release predates the contract."""
    declared = pair.manifest_pair(args.manifest)
    if declared is None:
        raise Abort(
            "the manifest declares no standby pair — the "
            "incumbent-consultation leg has nothing to exercise"
        )
    with open(args.model) as handle:
        model = json.load(handle)
    target = tune_carryover.tune_target(model)
    if target is None:
        raise Abort(
            "the emitted model declares no sequencer step_1_out "
            "parameter — the leg has no receipted state to carry"
        )
    component, parameter, point, _signal = target
    tune = {
        "set_parameter": {
            "component": component,
            "name": parameter,
            "value": TUNED,
        }
    }

    digest_entries, evidence, failures = [], {}, []
    rig = None
    probe_io = None
    relaunches = []
    try:
        rig = pair.launch_pair(args, declared)
        duty_url, standby_url = rig.duty_url, rig.standby_url
        incumbent = duty_url.removeprefix("http://")
        probe_io = simulate.PlantClient(rig.plant_addr)
        restartee_files = rig.standby_files
        state_file = restartee_files.get("state_file")
        if state_file is None or restartee_files.get("journal_file") is None:
            raise Abort(
                "the manifest's standby declares no "
                "state_file/journal_file — the leg has no stale "
                "checkpoint to restart onto"
            )

        # Phase 1 — convergence, and the incumbent's own recorded
        # claim token: the attribution the refusal's observed claimant
        # is read against and the holder the field's arbitration probe
        # names. A launch recording no token predates the claim
        # lifecycle this leg's arbitration reads.
        converged = rig.converge(failures)
        evidence["converged"] = converged["ticks"][-1]
        incumbent_token = failover.owner_token(rig.duty_preamble)
        if incumbent_token is None:
            raise Inconclusive(
                "the pinned release predates the recorded startup claim",
                "the field owner's startup log carries no "
                "write-ownership claim line — the refusal's observed "
                "claimant attribution and the field's arbitration "
                "probe have nothing to read",
            )
        digest_entries.append(
            {
                "phase": "converge",
                "ticks": converged["ticks"],
                "duty_role": converged["duty_role"],
                "standby_role": converged["standby_role"],
            }
        )

        # Phase 2 — the incumbent's receipted state: a tune through the
        # declared writable configuration point, settled into one
        # adopted receipt log and live on both peers' images. The
        # un-tuned served value is the control a rolled-back restart
        # would read back.
        schema = pair.get(f"{duty_url}/schema", "GET /schema", failures)
        interface = next(
            (
                entry.get("interface", {})
                for entry in schema.get("interfaces", [])
                if entry.get("name") == component
            ),
            {},
        )
        spec = next(
            (
                offered
                for offered in interface.get("commands", [])
                if offered.get("name") == f"set_parameter:{parameter}"
            ),
            None,
        )
        if not isinstance(spec, dict) or spec.get("adapted") != (
            "set_parameter"
        ):
            failures.append(
                f"the served registry declares no set_parameter:"
                f"{parameter} command on {component} — the tune target "
                "is not a writable configuration point"
            )
            raise Abort
        owner = converged["owner"]
        control = tune_carryover.parameter_value(owner, component, parameter)
        control_sample = simulate.snapshot_point(owner, point)
        if control is None or control_sample is None:
            failures.append(
                f"the tune target {component}'s {parameter} serves no "
                f"parameter value, or the out point {point} no sample"
            )
            raise Abort
        if control == TUNED:
            failures.append(
                f"the tune target {component}'s {parameter} already "
                f"reads {TUNED} — arming the incumbent with it proves "
                "nothing"
            )
            raise Abort
        status, receipt = pair.request(
            f"{duty_url}/command", {"command": tune, "actor": ACTOR}
        )
        if status != 200 or simulate.receipt_outcome(receipt) != "accepted":
            failures.append(
                f"the receipted tune answered {status} {receipt}, "
                "expected an accepted receipt"
            )
            raise Abort
        tracked, owner = pair.tick(standby_url, duty_url, failures)
        receipts = tune_carryover.identical_receipts(
            duty_url, standby_url, failures
        )
        if not any(
            entry.get("command") == tune
            and simulate.receipt_outcome(entry) == "applied"
            for entry in receipts
        ):
            failures.append(
                "the receipted tune never settled applied into the "
                "incumbent's adopted receipt log"
            )
            raise Abort
        armed = []
        for report, which in ((owner, "the incumbent's"),
                              (tracked, "the tracking peer's")):
            served = tune_carryover.parameter_value(
                report, component, parameter
            )
            if served != TUNED:
                failures.append(
                    f"{which} parameter report reads {served} for "
                    f"{component}'s {parameter} — the accepted tune is "
                    f"not live, expected {TUNED}"
                )
            armed.append(served)
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "arm",
                "component": component,
                "parameter": parameter,
                "point": point,
                "control": {"value": control, "sample": control_sample},
                "receipt": receipt,
                "armed": armed,
            }
        )

        # Phase 3 — the downtime: the demoted peer's container stops
        # while the incumbent keeps scanning, so the incumbent's line
        # stands strictly ahead of the persisted checkpoint the
        # restartee resumes — the gap a stale-baseline seizure rolls
        # back.
        baseline = persisted_checkpoint(state_file, failures)
        evidence["baseline"] = baseline["tick"]
        pair.stop(rig.standby)
        for _ in range(DOWNTIME_SCANS):
            owner = pair.scan(duty_url, failures)
        incumbent_report = pair.get(f"{duty_url}/role", "GET /role", failures)
        if incumbent_report.get("role") != "active":
            failures.append(
                f"the incumbent reports "
                f"{incumbent_report.get('role')!r} through the downtime "
                "window, expected active"
            )
            raise Abort
        if not isinstance(owner.get("tick"), int) or owner["tick"] <= (
            baseline["tick"] or 0
        ):
            # The precondition the whole leg rests on: a restart
            # resuming that checkpoint cannot seize the field without
            # reverting the incumbent's line, so a gap that never
            # opened makes both halves assert nothing.
            failures.append(
                "the incumbent's line stood at tick "
                f"{owner.get('tick')} across the downtime while the "
                "restartee's declared --state-file persisted tick "
                f"{baseline['tick']} — the stale-baseline restart this "
                "leg stages rolled back nothing"
            )
            raise Abort
        evidence["incumbent_tick"] = owner["tick"]
        digest_entries.append(
            {
                "phase": "downtime",
                "baseline": baseline["tick"],
                "high_water": baseline["high_water"],
                "incumbent_tick": owner["tick"],
                "duty_role": incumbent_report,
            }
        )

        # Phase 4 — the refusal over the live incumbent: the demoted
        # peer's declared persistence relaunched as an active naming
        # the reachable incumbent as its `--peer`.
        refusal_process, refusal_url, refusal_preamble = relaunch(
            "the restartee", args, rig.plant_addr, restartee_files,
            incumbent, relaunches,
        )
        resumed = resumed_tick(refusal_preamble)
        if resumed != baseline["tick"]:
            failures.append(
                f"the restartee resumed at tick {resumed} while its "
                f"declared --state-file persisted {baseline['tick']} — "
                "the stale-baseline restart this leg stages never "
                "happened"
            )
            raise Abort
        refusal_life = latest_lifetime(
            pair.get(f"{refusal_url}/journal", "GET /journal", failures)
        )
        consulted(refusal_life["entries"], "the live-incumbent restart's")
        refusal_report = pair.get(
            f"{refusal_url}/role", "GET /role", failures
        )
        if tamper == "expect-seized":
            # The doctored expectation — the defect shape the contract
            # closed: the restart over a live incumbent seizes the
            # field and the incumbent's receipted tune reverts to the
            # pre-restart value. The honest run's answered verdicts
            # must fail it.
            rolled = tune_carryover.parameter_value(
                pair.get(
                    f"{refusal_url}/snapshot", "GET /snapshot", failures
                ),
                component,
                parameter,
            )
            failures.append(
                f"{TAMPER_EVIDENCE}, and revert the incumbent's "
                f"receipted tune to the control {control} — the honest "
                f"run stood the launch down as "
                f"{refusal_report.get('role')!r} under the "
                f"{sync_word(refusal_report)} verdict with the tune "
                f"still reading {rolled}"
            )
            raise Abort
        carried, detail = tune_carried(
            [
                (refusal_url, "the relaunched run's"),
                (duty_url, "the incumbent's"),
            ],
            component,
            parameter,
            point,
            tune,
            failures,
        )
        refusal_served = {
            "boundary": refusal_life,
            "consult": first_event(
                refusal_life["entries"], "restart_consult"
            ),
            "refusal": first_event(
                refusal_life["entries"], "startup_claim_refused"
            ),
            "observed": first_event(
                refusal_life["entries"], "field_claim_observed"
            ),
            "entries": refusal_life["entries"],
            "role": refusal_report,
            "holder": holder_word(
                claim_reclaim.verdict_owner(
                    probe_io.request({"op": "step", "dt": 0})
                ),
                {incumbent_token: "incumbent"},
            ),
            "carried": carried,
            "carried_detail": detail,
            "persisted": baseline["tick"],
            "incumbent_tick": owner["tick"],
        }
        marks = replay(
            "refusal-over-live-incumbent", refusal_served, failures
        )
        marks["generation"] = resume_adoption(
            refusal_url, baseline, failures
        )
        adopted = (refusal_served["consult"].get("outcome") or {}).get(
            "adopted"
        ) or {}
        if adopted.get("resumed_at") != owner["tick"]:
            failures.append(
                "the consult adopted the incumbent's line at tick "
                f"{adopted.get('resumed_at')}, while the incumbent's "
                f"served line stood at tick {owner['tick']} — the "
                "consult did not carry the reachable incumbent's live "
                "line forward"
            )
        marks["attributed"] = holder_word(
            (refusal_served["observed"] or {}).get("claimant"),
            {incumbent_token: "incumbent"},
        )
        if marks["attributed"] != "incumbent":
            failures.append(
                "the refused grant's observed claimant is the "
                f"{marks['attributed']} claim, not the incumbent's own "
                f"recorded token: {refusal_served['observed']}"
            )
        marks["role"] = str(refusal_report.get("role"))
        marks["sync"] = sync_word(refusal_report)
        if (
            refusal_report.get("role") != "standby"
            or marks["sync"] != "unsynchronized"
        ):
            failures.append(
                "the refused launch's served surface reads "
                f"{json.dumps(refusal_report)[:300]} — expected role "
                "standby under the unsynchronized verdict, the "
                "recorded rejoin disposition"
            )
        if failures:
            raise Abort
        digest_entries.append({"phase": "refusal", **marks})

        # Phase 5 — the refused launch is a working pair member: its
        # pulls reconverge it onto the incumbent's line, which the
        # driven pair tick's identical-image assertion reads.
        reconvergence = []
        rejoined = False
        for _ in range(RECONVERGE_SCANS):
            pair.tick(refusal_url, duty_url, failures)
            report = pair.get(f"{refusal_url}/role", "GET /role", failures)
            reconvergence.append(
                f"{report.get('role')}/{sync_word(report)}"
            )
            if claim_reclaim.tracking(report):
                rejoined = True
                break
        evidence["reconvergence"] = reconvergence
        if not rejoined:
            failures.append(
                "the refused launch never reconverged onto the "
                f"incumbent's line — its sync stayed {reconvergence}"
            )
            raise Abort
        digest_entries.append(
            {
                "phase": "rejoin",
                "sync": reconvergence,
                "duty_role": pair.get(
                    f"{duty_url}/role", "GET /role", failures
                ),
            }
        )

        # Phase 6 — the seizure over the dead incumbent: the refused
        # launch stops, the incumbent's container stops, and the same
        # declared persistence relaunches with the dead incumbent still
        # named as its `--peer`.
        pair.stop(refusal_process)
        pair.stop(rig.duty)
        seized = persisted_checkpoint(state_file, failures)
        seize_process, seize_url, seize_preamble = relaunch(
            "the restartee over the dead incumbent", args, rig.plant_addr,
            restartee_files, incumbent, relaunches,
        )
        restartee = seize_url.removeprefix("http://")
        restartee_token = failover.owner_token(seize_preamble)
        if restartee_token is None:
            failures.append(
                "the seizing launch recorded no owner-token claim line "
                "— the takeover's grant verdict names no holder: "
                f"{'; '.join(seize_preamble) or 'no diagnostic'}"
            )
            raise Abort
        resumed = resumed_tick(seize_preamble)
        if resumed != seized["tick"]:
            failures.append(
                f"the restartee resumed at tick {resumed} while its "
                f"declared --state-file persisted {seized['tick']}"
            )
            raise Abort
        seize_life = latest_lifetime(
            pair.get(f"{seize_url}/journal", "GET /journal", failures)
        )
        consulted(seize_life["entries"], "the dead-incumbent takeover's")
        ticks = []
        for _ in range(SEIZE_SCANS):
            ticks.append(pair.scan(seize_url, failures)["tick"])
        seize_report = pair.get(f"{seize_url}/role", "GET /role", failures)
        carried, detail = tune_carried(
            [(seize_url, "the seizing run's")],
            component,
            parameter,
            point,
            tune,
            failures,
        )
        seize_served = {
            "boundary": seize_life,
            "consult": first_event(
                seize_life["entries"], "restart_consult"
            ),
            "refusal": first_event(
                seize_life["entries"], "startup_claim_refused"
            ),
            "observed": first_event(
                seize_life["entries"], "field_claim_observed"
            ),
            "entries": seize_life["entries"],
            "role": seize_report,
            "holder": holder_word(
                claim_reclaim.verdict_owner(
                    probe_io.request({"op": "step", "dt": 0})
                ),
                {restartee_token: "restartee"},
            ),
            "carried": carried,
            "carried_detail": detail,
            "persisted": seized["tick"],
            "incumbent_tick": owner["tick"],
        }
        marks = replay("seizure-over-dead-incumbent", seize_served, failures)
        marks["generation"] = resume_adoption(seize_url, seized, failures)
        unadopted = (seize_served["consult"].get("outcome") or {}).get(
            "unadopted"
        ) or {}
        if not str(unadopted.get("detail", "")).startswith(
            "incumbent checkpoint pull failed"
        ):
            failures.append(
                "the unanswered consult journalled the detail "
                f"{unadopted.get('detail')!r} — the consultation ledger "
                "must name why no incumbent line was adopted"
            )
        if seize_served["refusal"] is not None:
            failures.append(
                "the seizing lifetime journalled a refused startup "
                "grant — the dead owner's standing claim is what the "
                "recovery preempts: "
                f"{json.dumps(seize_served['refusal'])[:300]}"
            )
        if seize_served["observed"] is not None:
            failures.append(
                "the seizing lifetime observed a foreign standing claim "
                "— no live holder answers the field it took: "
                f"{json.dumps(seize_served['observed'])[:300]}"
            )
        if seize_report.get("role") != "active":
            failures.append(
                f"the seizing run reports role "
                f"{seize_report.get('role')!r} — a granted startup "
                "claim owns the field from startup"
            )
        if failures:
            raise Abort
        digest_entries.append(
            {"phase": "seizure", **marks, "ticks": ticks}
        )

        # Phase 7 — the restore: the incumbent relaunches on its own
        # declared persistence as a `--standby` of the current field
        # owner — the remedy the refusal names — at its original monitor
        # address, so the owner under restore's configured tracking
        # name answers again, and the documented switch seats the
        # declared duty member back as field owner.
        rig.duty, duty_url, duty_preamble = pair.spawn_peer(
            args.controller,
            args.model,
            args.dt,
            rig.plant_addr,
            restartee,
            rig.duty_files,
            listen=incumbent,
            pair_token=pair.PAIR_TOKEN,
        )
        rig.duty_url = duty_url
        if duty_url is None:
            failures.append(
                "the rejoining standby exited at startup: "
                f"{'; '.join(duty_preamble[-2:]) or 'no diagnostic'}"
            )
            raise Abort
        rejoin = []
        converged_back = False
        for _ in range(RESTORE_SCANS):
            # Tracking-first, the pair harness's driven order: the
            # rejoining standby pulls the current owner's latest
            # checkpoint before the owner writes again.
            pair.scan(duty_url, failures)
            pair.scan(seize_url, failures)
            report = pair.get(f"{duty_url}/role", "GET /role", failures)
            rejoin.append(f"{report.get('role')}/{sync_word(report)}")
            if claim_reclaim.tracking(report):
                converged_back = True
                break
        if not converged_back:
            failures.append(
                "the rejoining standby never reconverged onto the "
                f"current field owner — its sync stayed {rejoin}"
            )
            raise Abort
        rig.demote(seize_url, failures, "the seized field owner")
        rig.promote(duty_url, failures, "the rejoining duty member")
        seated = []
        restored = False
        for _ in range(RESTORE_SCANS):
            # The demoted owner now tracks the promoted one, so the
            # driven tick scans it first.
            pair.tick(seize_url, duty_url, failures)
            duty_report = pair.get(f"{duty_url}/role", "GET /role", failures)
            owner_report = pair.get(
                f"{seize_url}/role", "GET /role", failures
            )
            seated.append(
                f"{duty_report.get('role')}/{owner_report.get('role')}"
            )
            if duty_report.get("role") == "active" and (
                claim_reclaim.tracking(owner_report)
            ):
                restored = True
                break
        evidence["restore"] = seated
        if not restored:
            failures.append(
                "the pair's launch roles never restored — the role "
                f"reports stayed {seated}"
            )
            raise Abort
        final = pair.get(f"{duty_url}/snapshot", "GET /snapshot", failures)
        final_role = pair.get(f"{duty_url}/role", "GET /role", failures)
        if tune_carryover.parameter_value(
            final, component, parameter
        ) != TUNED:
            failures.append(
                "the restored duty member's parameter report does not "
                f"carry the receipted tune — expected {TUNED}"
            )
        if final_role.get("field_claim") != "held":
            failures.append(
                f"the restored field owner reports claim "
                f"{final_role.get('field_claim')!r} — the switch left "
                "the field unclaimed"
            )
        if failures:
            raise Abort
        digest_entries.append(
            {
                "phase": "restore",
                "rejoin": rejoin,
                "seated": seated,
                "duty_role": final_role,
                "final_tick": final["tick"],
            }
        )
        evidence["final_tick"] = final["tick"]
    except Inconclusive:
        raise
    except Abort as abort:
        failures.extend(str(argument) for argument in abort.args)
    except Exception as error:
        failures.append(f"the run raised {error!r}")
    finally:
        for process in relaunches:
            pair.stop(process)
        if probe_io is not None:
            probe_io.close()
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
        choices=["expect-seized"],
        help="doctor the leg's own expectation to the defect shape — a "
        "restart over a live incumbent seizing the field and reverting "
        "the incumbent's receipted state — so the pass must fail naming "
        "the honest refusal",
    )
    args = parser.parse_args()

    with open(args.scenario) as handle:
        args.dt = json.load(handle)["dt"]

    try:
        digest_entries, evidence, failures = incumbent_consultation_pass(
            args, args.tamper
        )
    except Inconclusive as inconclusive:
        if args.tamper is not None:
            eprint(
                f"incumbent-consultation: {TAMPER_EVIDENCE} — an "
                "inconclusive run offers the doctored case no evidence"
            )
            return 1
        # The digest line carries only the stable reason — the run's own
        # verdicts report on stderr, where two identical passes need not
        # share them.
        reason = inconclusive.args[0]
        detail = inconclusive.args[-1]
        eprint(f"incumbent-consultation: inconclusive — {detail}")
        print(f"incumbent-consultation-digest inconclusive — {reason}")
        return 0
    except Abort as abort:
        for line in abort.args:
            eprint(f"incumbent-consultation: {line}")
        return 1
    for failure in failures:
        eprint(f"incumbent-consultation: {failure}")
    if args.tamper is not None:
        if not failures:
            eprint(
                f"incumbent-consultation: the {args.tamper} case passed "
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
        f"incumbent-consultation-digest {digest} — converged at tick "
        f"{evidence['converged']} with the incumbent's receipted tune "
        "live on both peers, its line moving to tick "
        f"{evidence['incumbent_tick']} across the downtime the "
        f"restartee's baseline at tick {evidence['baseline']} left "
        "behind; the live-incumbent restart adopted that line and its "
        "startup claim refused by the named verdict with the "
        "incumbent's receipted state intact, and reconverged to "
        "tracking; with the incumbent stopped the restartee seized on "
        "its journaled resumed baseline and consultation ledger, and "
        "the pair's launch roles restored at tick "
        f"{evidence['final_tick']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())